#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歌单分析器：解析主流音乐平台歌单链接 -> 多维度分析 -> 输出 Markdown / HTML 报告。

支持平台：网易云音乐(music.163.com)、QQ音乐(y.qq.com)、酷狗(kugou.com)、酷我(kuwo.cn)。
分析维度：基本信息 / 时长 / 年代 / 歌手 / 语言 / 热度 / 曲风 / 歌曲类型 / 情绪 /
          专辑 / 集中度(HHI) / 排序逻辑 / 歌手代际 / 合作检测 / 口味画像 / 词云 / 洞察。

用法：
    python3 analyze_playlist.py "<歌单链接>" [-o 输出目录]

输出：report.md + report.html + analysis.json（同目录）。
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先执行: pip install requests")

try:
    import jinja2
except ImportError:
    sys.exit("缺少依赖 jinja2，请先执行: pip install jinja2")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 20

# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


class Song:
    def __init__(self, title: str, artists: List[str], album: str = "",
                 duration_ms: int = 0, popularity: Optional[int] = None,
                 publish_year: Optional[int] = None, url: str = ""):
        self.title = title
        self.artists = artists
        self.album = album
        self.duration_ms = duration_ms
        self.popularity = popularity
        self.publish_year = publish_year
        self.url = url

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "popularity": self.popularity,
            "publish_year": self.publish_year,
            "url": self.url,
        }


class Playlist:
    def __init__(self, platform: str, name: str, creator: str = "",
                 description: str = "", tags: List[str] = None,
                 play_count: int = 0, track_count: int = 0,
                 created_time: Optional[int] = None, songs: Optional[List[Song]] = None):
        self.platform = platform
        self.name = name
        self.creator = creator
        self.description = description
        self.tags = tags or []
        self.play_count = play_count
        self.track_count = track_count
        self.created_time = created_time
        self.songs = songs or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "name": self.name,
            "creator": self.creator,
            "description": self.description,
            "tags": self.tags,
            "play_count": self.play_count,
            "track_count": self.track_count,
            "created_time": self.created_time,
            "songs": [s.to_dict() for s in self.songs],
        }


def load_playlist_from_data(path: str) -> Playlist:
    """从 --data-only 导出的 playlist.json 重建 Playlist（保证 index 与 AI 标注一致）。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    meta = d.get("meta", {})
    songs = []
    for s in d.get("songs", []):
        songs.append(Song(
            title=s.get("title", ""),
            artists=list(s.get("artists") or []),
            album=s.get("album", ""),
            duration_ms=int(s.get("duration_ms", 0) or 0),
            popularity=s.get("popularity"),
            publish_year=s.get("year"),
            url=s.get("url", ""),
        ))
    return Playlist(
        platform=meta.get("platform", ""),
        name=meta.get("name", ""),
        creator=meta.get("creator", ""),
        description=meta.get("description", ""),
        tags=meta.get("tags") or [],
        play_count=int(meta.get("play_count", 0) or 0),
        track_count=int(meta.get("track_count", 0) or len(songs)),
        created_time=meta.get("created_time"),
        songs=songs,
    )


# --------------------------------------------------------------------------- #
# 平台识别与 ID 提取
# --------------------------------------------------------------------------- #

def detect_platform(url: str) -> Optional[str]:
    if "music.163.com" in url:
        return "netease"
    if "y.qq.com" in url or "c.y.qq.com" in url:
        return "qq"
    if "kugou.com" in url:
        return "kugou"
    if "kuwo.cn" in url:
        return "kuwo"
    return None


def extract_id(platform: str, url: str) -> Optional[str]:
    patterns = {
        "netease": [r"playlist\?id=(\d+)", r"playlist/(\d+)", r"#/playlist\?id=(\d+)"],
        "qq": [r"playlist/(\d+)", r"taoge\.html\?id=(\d+)", r"disstid=(\d+)"],
        "kugou": [r"special/single/(\d+)", r"plist/list/(\d+)"],
        "kuwo": [r"playlist_detail/(\d+)"],
    }
    for pat in patterns.get(platform, []):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# 各平台抓取适配器
# --------------------------------------------------------------------------- #

def fetch_netease(pid: str) -> Playlist:
    """网易云歌单。detail 接口 tracks 上限 1000 首，故用全量 trackIds 分批取详情。"""
    url = "https://music.163.com/api/v6/playlist/detail"
    params = {"id": pid, "n": 100000, "s": 8}
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/",
               "Cookie": "os=pc; appver=8.0.0; osver=win"}
    r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    pl = r.json()["playlist"]
    track_ids = [t.get("id") for t in (pl.get("trackIds") or []) if t.get("id")]
    if not track_ids:
        track_ids = [t.get("id") for t in pl.get("tracks", []) if t.get("id")]

    songs = []
    for start in range(0, len(track_ids), 500):
        batch = track_ids[start:start + 500]
        try:
            r = requests.post(
                "https://music.163.com/api/v3/song/detail",
                data={"c": json.dumps([{"id": i} for i in batch])},
                headers=headers, timeout=TIMEOUT)
            fetched = r.json().get("songs", [])
        except (ValueError, requests.exceptions.RequestException):
            fetched = []
        fetched_by_id = {t.get("id"): t for t in fetched}
        for i in batch:
            t = fetched_by_id.get(i)
            if not t:
                continue
            artists = [a.get("name", "") for a in t.get("ar", []) if a.get("name")]
            album = (t.get("al") or {}).get("name", "")
            year = None
            pt = t.get("publishTime") or t.get("publishDate")
            if pt:
                try:
                    year = datetime.fromtimestamp(pt / 1000).year
                except (ValueError, OSError):
                    year = None
            songs.append(Song(
                title=t.get("name", ""),
                artists=artists,
                album=album,
                duration_ms=int(t.get("dt", 0) or 0),
                popularity=t.get("pop"),
                publish_year=year,
                url="https://music.163.com/#/song?id={}".format(i),
            ))
    creator = (pl.get("creator") or {}).get("nickname", "")
    desc = (pl.get("description") or "").strip()
    if len(desc) > 300:
        desc = desc[:300] + "…"
    return Playlist(
        platform="netease",
        name=pl.get("name", ""),
        creator=creator,
        description=desc,
        tags=pl.get("tags") or [],
        play_count=int(pl.get("playCount", 0) or 0),
        track_count=len(track_ids),
        created_time=pl.get("createTime"),
        songs=songs,
    )


def fetch_qq(pid: str) -> Playlist:
    url = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
    params = {"type": 1, "json": 1, "utf8": 1, "onlysong": 0,
              "disstid": pid, "format": "json", "g_tk": 5381}
    headers = {"User-Agent": UA, "Referer": "https://y.qq.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    cdlist = r.json().get("cdlist") or []
    if not cdlist:
        raise ValueError("QQ音乐未返回歌单数据，链接可能无效或已下架")
    c = cdlist[0]
    songs = []
    for s in c.get("songlist", []):
        artists = [x.get("name", "") for x in s.get("singer", []) if x.get("name")]
        interval = int(s.get("interval", 0) or 0)
        mid = s.get("songmid", "")
        songs.append(Song(
            title=s.get("songname", ""),
            artists=artists,
            album=s.get("albumname", ""),
            duration_ms=interval * 1000,
            popularity=None,
            publish_year=None,
            url="https://y.qq.com/n/ryqq/songDetail/{}".format(mid) if mid else "",
        ))
    tags = []
    for t in (c.get("diss_tags") or []):
        if isinstance(t, dict):
            tags.append(t.get("name", ""))
        elif isinstance(t, str):
            tags.append(t)
    return Playlist(
        platform="qq",
        name=c.get("dissname", ""),
        creator=(c.get("creator") or {}).get("name", ""),
        description=(c.get("desc") or "").strip()[:300],
        tags=tags,
        play_count=int(c.get("listen_num", 0) or 0),
        track_count=int(c.get("songnum", 0) or len(songs)),
        created_time=None,
        songs=songs,
    )


def fetch_kugou(pid: str) -> Playlist:
    url = "https://m.kugou.com/plist/list/{}".format(pid)
    params = {"json": "true"}
    headers = {"User-Agent": UA, "Referer": "https://www.kugou.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text

    # 新版酷狗 m 站返回 HTML，曲目在 <li><a title="歌手 - 歌名" ... data="HASH|时长">
    items = re.findall(
        r'<li><a[^>]*title="([^"]*)"[^>]*href="(https://www\.kugou\.com/mixsong/[^"]+)"[^>]*data="([^"]+)"',
        text)
    songs = []
    if items:
        for title, song_url, data in items:
            duration_ms = 0
            if "|" in data:
                try:
                    duration_ms = int(data.split("|")[1])
                except ValueError:
                    duration_ms = 0
            title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            artists = []
            songname = title
            m = re.match(r"^(.*?)\s*-\s*(.*)$", title)
            if m:
                artists = [m.group(1).strip()]
                songname = m.group(2).strip()
            songs.append(Song(title=songname, artists=artists,
                              duration_ms=duration_ms, url=song_url))
    else:
        # 兼容旧版 JSON 接口
        try:
            data = r.json()
        except ValueError:
            data = {}
        if data.get("status") != 1:
            raise ValueError("酷狗未返回歌单数据，链接可能无效或已被改版")
        pl = data.get("plist", {})
        for s in (pl.get("list") or {}).get("info", []):
            songname = s.get("songname", "")
            singername = s.get("singername", "")
            if not songname and s.get("filename"):
                m = re.match(r"^(.*?)\s*-\s*(.*)$", s.get("filename", ""))
                if m:
                    singername = m.group(1).strip()
                    songname = m.group(2).strip()
            duration = int(s.get("duration", 0) or 0)
            hash_id = s.get("hash", "")
            songs.append(Song(title=songname, artists=[singername] if singername else [],
                              album=s.get("album_name", ""),
                              duration_ms=duration * 1000,
                              url="https://www.kugou.com/song/#hash={}".format(hash_id) if hash_id else ""))

    # 歌单元信息
    name, creator, intro = "", "", ""
    mt = re.search(r"<title>([^<]*)", text)
    if mt:
        name = mt.group(1).split("_")[0].strip()
    mc = re.search(r"创建人[:：]\s*</span>\s*([^<]*)", text) or re.search(r"创建者[:：]\s*</span>\s*<span>([^<]*)", text)
    if mc:
        creator = mc.group(1).strip()
    mi = re.search(r"简介[:：]</span>([^<]*)", text)
    if mi:
        intro = mi.group(1).strip()[:300]

    if not songs:
        raise ValueError("酷狗歌单为空或解析失败")
    return Playlist(
        platform="kugou",
        name=name or "酷狗歌单 {}".format(pid),
        creator=creator,
        description=intro,
        tags=[],
        play_count=0,
        track_count=len(songs),
        created_time=None,
        songs=songs,
    )


def fetch_kuwo(pid: str) -> Playlist:
    """酷我歌单。反爬校验较严：先取首页 cookie，再尝试多组 csrf 方案。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Accept": "application/json, text/plain, */*",
                         "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        sess.get("https://www.kuwo.cn/", timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        pass

    base_cookies = dict(sess.cookies)
    url = "https://www.kuwo.cn/api/www/playlist/playListInfo"
    params = {"pid": pid, "pn": 1, "rn": 10000}

    attempts = []
    # 方案1：首页 cookie 里的 kw_token
    csrf1 = sess.cookies.get("kw_token", "") or ""
    attempts.append(("首页kw_token", csrf1, base_cookies))
    # 方案2：随机固定值同时作为 csrf 和 kw_token
    token2 = "T" + "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=9))
    c2 = dict(base_cookies)
    c2["kw_token"] = token2
    attempts.append(("自造csrf", token2, c2))
    # 方案3：Hm_token + Cross 签名
    hm3 = "".join(random.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=32))
    cross3 = hashlib.md5(hashlib.sha1(hm3.encode()).hexdigest().encode()).hexdigest()
    token3 = "T" + "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=9))
    c3 = dict(base_cookies)
    c3["kw_token"] = token3
    c3["Hm_token"] = hm3
    attempts.append(("Hm_token+Cross", token3, c3, cross3))

    last_err = ""
    for attempt in attempts:
        label, csrf, cookies = attempt[0], attempt[1], attempt[2]
        cross = attempt[3] if len(attempt) > 3 else None
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.kuwo.cn/playlist_detail/{}".format(pid),
            "csrf": csrf,
        }
        if cross:
            headers["Cross"] = cross
        try:
            r = sess.get(url, params=params, headers=headers, cookies=cookies, timeout=TIMEOUT)
            data = r.json()
            if data.get("success") is False:
                last_err = data.get("message", "unknown")
                continue
            d = data.get("data")
            if data.get("code") != 200 or not d:
                last_err = "code={}".format(data.get("code"))
                continue
            songs = []
            for s in (d.get("list") or []):
                rid = s.get("rid", "")
                duration_ms = 0
                stm = s.get("songTimeMinutes") or s.get("duration") or 0
                if isinstance(stm, str) and ":" in stm:
                    mm, ss = stm.split(":")[:2]
                    try:
                        duration_ms = (int(mm) * 60 + int(ss)) * 1000
                    except ValueError:
                        duration_ms = 0
                elif isinstance(stm, (int, float)) and float(stm) > 100:
                    duration_ms = int(float(stm) * 1000)
                else:
                    try:
                        duration_ms = int(float(stm) * 1000)
                    except (ValueError, TypeError):
                        duration_ms = 0
                songs.append(Song(
                    title=s.get("name", ""),
                    artists=[s.get("artist", "")] if s.get("artist") else [],
                    album=s.get("album", ""),
                    duration_ms=duration_ms,
                    popularity=None,
                    publish_year=None,
                    url="https://www.kuwo.cn/play_detail/{}".format(rid) if rid else "",
                ))
            if not songs:
                raise ValueError("酷我歌单为空或解析失败")
            return Playlist(
                platform="kuwo",
                name=d.get("playlistName", ""),
                creator=d.get("nickname", ""),
                description="",
                tags=[],
                play_count=int(d.get("listenCount", 0) or 0),
                track_count=len(songs),
                created_time=None,
                songs=songs,
            )
        except requests.exceptions.RequestException as e:
            last_err = str(e)

    raise ValueError("酷我反爬校验失败（{}），接口可能已升级，建议改用网易云/QQ音乐链接".format(last_err))


FETCHERS = {"netease": fetch_netease, "qq": fetch_qq,
            "kugou": fetch_kugou, "kuwo": fetch_kuwo}
PLATFORM_NAMES = {"netease": "网易云音乐", "qq": "QQ音乐",
                  "kugou": "酷狗音乐", "kuwo": "酷我音乐"}


# --------------------------------------------------------------------------- #
# 语言检测
# --------------------------------------------------------------------------- #

def _char_script(ch: str) -> str:
    o = ord(ch)
    if unicodedata.name(ch, "").startswith("CJK UNIFIED"):
        return "zh"
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF:
        return "ko"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "ja"
    if 0x0400 <= o <= 0x052F:
        return "ru"
    if ch.isascii() and ch.isalpha():
        return "en"
    return "other"


def detect_language(title: str, artists: List[str]) -> str:
    text = title + " " + " ".join(artists)
    counts: Counter = Counter(_char_script(c) for c in text if c.strip())
    if not counts:
        return "其他"
    score = {"zh": 1.0, "ja": 1.5, "ko": 1.5, "ru": 1.5, "en": 0.35, "other": 0.1}
    ranked = sorted(counts.items(), key=lambda kv: kv[1] * score.get(kv[0], 0.1), reverse=True)
    label = ranked[0][0]
    return {"zh": "中文", "ja": "日语", "ko": "韩语", "ru": "俄语", "en": "英语", "other": "其他"}[label]


# --------------------------------------------------------------------------- #
# 曲风 / 类型 / 情绪 词典推理
# --------------------------------------------------------------------------- #

GENRES = [
    ("流行", ["pop", "流行", "网络"]),
    ("摇滚", ["rock", "摇滚"]),
    ("民谣", ["folk", "民谣", "indie"]),
    ("电子", ["electronic", "edm", "house", "techno", "trance", "电子", "电音", "dance"]),
    ("说唱嘻哈", ["rap", "hip-hop", "hiphop", "说唱", "嘻哈"]),
    ("国风古风", ["古风", "国风", "中国风", "戏腔"]),
    ("爵士", ["jazz", "爵士"]),
    ("蓝调灵魂", ["blues", "蓝调", "soul", "r&b", "rnb", "节奏布鲁斯"]),
    ("金属", ["metal", "金属"]),
    ("古典", ["classical", "古典", "交响", "协奏", "奏鸣"]),
    ("轻音乐", ["new age", "轻音乐", "新世纪", "冥想"]),
    ("纯音乐", ["instrumental", "纯音乐", "钢琴曲", "piano solo"]),
    ("影视原声", ["ost", "soundtrack", "原声", "影视"]),
    ("儿歌", ["儿歌", "童谣"]),
]

TYPES = [
    ("纯音乐", ["纯音乐", "instrumental", "钢琴曲", "伴奏", "piano solo", "纯钢琴"]),
    ("翻唱", ["cover", "翻唱"]),
    ("现场版", ["live", "现场", "live版", "演唱会"]),
    ("混音版", ["remix", "混音", "dj版", "慢摇"]),
    ("原声带", ["ost", "soundtrack", "原声"]),
    ("合唱", ["合唱"]),
]

# mood: (标签, 关键词, valence(0悲观-1乐观), energy(0静-1动))
MOODS = [
    ("治愈温暖", ["治愈", "温暖", "温柔", "陪伴", "阳光", "晴天", "暖心"], 0.78, 0.35),
    ("燃热血", ["燃", "热血", "战斗", "追梦", "逆袭", "不服", "炸裂"], 0.6, 0.95),
    ("伤感", ["失恋", "难过", "心碎", "伤感", "流泪", "泪", "孤独", "寂寞", "遗憾", "悲伤"], 0.2, 0.25),
    ("甜恋", ["甜蜜", "恋爱", "告白", "心动", "情歌", "喜欢你", "爱你"], 0.85, 0.6),
    ("深夜静谧", ["深夜", "失眠", "睡前", "安眠", "静", "夜", "月光"], 0.45, 0.15),
    ("动感", ["动感", "dance", "派对", "蹦迪", "活力", "运动"], 0.7, 0.85),
    ("轻快愉悦", ["轻快", "快乐", "开心", "欢快", "元气", "彩虹"], 0.8, 0.6),
]


def classify_genre(title: str, album: str, artists: List[str]) -> str:
    text = "{} {} {}".format(title, album, " ".join(artists)).lower()
    for genre, kws in GENRES:
        if any(k in text for k in kws):
            return genre
    return "流行/其他"


def classify_type(title: str) -> str:
    t = title.lower()
    for typ, kws in TYPES:
        if any(k in t for k in kws):
            return typ
    return "录音室版"


def classify_mood(title: str) -> Tuple[str, float, float]:
    t = title.lower()
    for mood, kws, v, e in MOODS:
        if any(k in t for k in kws):
            return mood, v, e
    return "中性", 0.5, 0.5


def extract_collabs(title: str, artists: List[str]) -> List[str]:
    """识别标题中的 feat./&/× 合作者，以及多歌手曲目。"""
    partners = []
    m = re.search(r"(?:feat\.?|ft\.?|featuring)\s*[:：\-–]?\s*([^,，/&×()（）]+)", title, re.I)
    if m:
        name = m.group(1).strip()
        if name:
            partners.append(name)
    if len(artists) > 1:
        partners.extend(artists[1:])
    return partners


# --------------------------------------------------------------------------- #
# 统计工具
# --------------------------------------------------------------------------- #

def hhi(counter: Counter) -> float:
    total = sum(counter.values())
    if not total:
        return 0.0
    return sum((c / total) ** 2 for c in counter.values())


def spearman(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0

    def _rank(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0] * n
        for r, i in enumerate(order):
            ranks[i] = r + 1
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def _fmt_duration(ms: int) -> str:
    s = round(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "{}小时{}分{}秒".format(h, m, sec)
    if m:
        return "{}分{}秒".format(m, sec)
    return "{}秒".format(sec)


def _fmt_short_duration(ms: int) -> str:
    s = round(ms / 1000)
    return "{}:{:02d}".format(s // 60, s % 60)


def _fmt_count(n: int) -> str:
    if n >= 100000000:
        return "{:.2f}亿".format(n / 100000000)
    if n >= 10000:
        return "{:.1f}万".format(n / 10000)
    return str(n)


def _smooth(vals: List[float], window: int = 5) -> List[float]:
    if len(vals) <= window:
        return vals
    out = []
    for i in range(len(vals)):
        lo = max(0, i - window // 2)
        hi = min(len(vals), i + window // 2 + 1)
        out.append(sum(vals[lo:hi]) / (hi - lo))
    return out


def _pct_table(items: List[Tuple[Any, int]], total: int, fmt: str = "{:.1f}%") -> List[List[str]]:
    return [[str(k), str(v), fmt.format(v / total * 100)] for k, v in items]


# --------------------------------------------------------------------------- #
# 多维分析（A-E 全部维度）
# --------------------------------------------------------------------------- #

def analyze(pl: Playlist) -> Dict[str, Any]:
    songs = pl.songs
    n = len(songs)
    if n == 0:
        raise ValueError("歌单中没有可分析的曲目")

    durations = [s.duration_ms for s in songs if s.duration_ms]
    years = [s.publish_year for s in songs if s.publish_year]
    pops = [s.popularity for s in songs if s.popularity is not None]

    lead_artist = Counter()
    for s in songs:
        if s.artists:
            lead_artist[s.artists[0]] += 1
    artist_appearances = Counter(a for s in songs for a in s.artists if a)
    lang_counter = Counter(detect_language(s.title, s.artists) for s in songs)
    genre_counter = Counter(classify_genre(s.title, s.album, s.artists) for s in songs)
    type_counter = Counter(classify_type(s.title) for s in songs)
    album_counter = Counter(s.album for s in songs if s.album)
    mood_tags = [classify_mood(s.title) for s in songs]
    mood_counter = Counter(m for m, _, _ in mood_tags)

    # 时长直方图（分钟区间）
    duration_bins = [("0-2", 0), ("2-3", 0), ("3-4", 0), ("4-5", 0),
                     ("5-6", 0), ("6-8", 0), ("8+", 0)]
    for d in durations:
        m = d / 60000
        if m < 2: duration_bins[0] = (duration_bins[0][0], duration_bins[0][1] + 1)
        elif m < 3: duration_bins[1] = (duration_bins[1][0], duration_bins[1][1] + 1)
        elif m < 4: duration_bins[2] = (duration_bins[2][0], duration_bins[2][1] + 1)
        elif m < 5: duration_bins[3] = (duration_bins[3][0], duration_bins[3][1] + 1)
        elif m < 6: duration_bins[4] = (duration_bins[4][0], duration_bins[4][1] + 1)
        elif m < 8: duration_bins[5] = (duration_bins[5][0], duration_bins[5][1] + 1)
        else: duration_bins[6] = (duration_bins[6][0], duration_bins[6][1] + 1)

    # 年代分布 + 逐年计数
    year_counter = Counter(years)
    if years:
        lo, hi = (min(years) // 10) * 10, (max(years) // 10) * 10
        decade_bins = [(d, sum(1 for y in years if (y // 10) * 10 == d))
                       for d in range(lo, hi + 1, 10)]
        unknown_years = n - len(years)
    else:
        decade_bins = []
        unknown_years = n

    # 热度分布
    pop_bins = [("0-20", 0), ("20-40", 0), ("40-60", 0), ("60-80", 0), ("80-100", 0)]
    for p in pops:
        if p < 20: pop_bins[0] = (pop_bins[0][0], pop_bins[0][1] + 1)
        elif p < 40: pop_bins[1] = (pop_bins[1][0], pop_bins[1][1] + 1)
        elif p < 60: pop_bins[2] = (pop_bins[2][0], pop_bins[2][1] + 1)
        elif p < 80: pop_bins[3] = (pop_bins[3][0], pop_bins[3][1] + 1)
        else: pop_bins[4] = (pop_bins[4][0], pop_bins[4][1] + 1)

    # 歌手：首席 vs 出镜
    top_artists = artist_appearances.most_common(10)
    artist_stats = []
    for name, appear in top_artists:
        artist_stats.append({
            "name": name,
            "lead": lead_artist.get(name, 0),
            "appear": appear,
            "lead_share": round(lead_artist.get(name, 0) / n * 100, 1) if n else 0,
            "pct": round(appear / n * 100, 1) if n else 0,
        })
    top1_track_share = (lead_artist.most_common(1)[0][1] / n) if lead_artist else 0.0

    # 歌手代际（按首席歌曲年份中位数）
    artist_years: Dict[str, List[int]] = {}
    for s in songs:
        if s.artists and s.publish_year:
            artist_years.setdefault(s.artists[0], []).append(s.publish_year)
    cohort_counter = Counter()
    cohort_artist_counter = Counter()
    cohort_names = {
        0: "经典前辈(-1994)", 1: "中生代(95-04)", 2: "新生代(05-14)", 3: "新锐(15-)"}
    for name, ys in artist_years.items():
        med = statistics.median(ys)
        if med < 1995: c = 0
        elif med < 2005: c = 1
        elif med < 2015: c = 2
        else: c = 3
        cohort_artist_counter[cohort_names[c]] += 1
        cohort_counter[cohort_names[c]] += len(ys)

    # 语言多样性（归一化熵）
    probs = [c / n for c in lang_counter.values()]
    entropy = -sum(p * math.log(p) for p in probs if p > 0)
    lang_diversity = entropy / math.log(len(probs)) if len(probs) > 1 else 0.0

    # 曲风多样性
    gprobs = [c / n for c in genre_counter.values()]
    gentropy = -sum(p * math.log(p) for p in gprobs if p > 0)
    genre_diversity = gentropy / math.log(len(gprobs)) if len(gprobs) > 1 else 0.0

    # HHI 集中度
    conc = {
        "artist": round(hhi(lead_artist), 3),
        "album": round(hhi(album_counter), 3),
        "language": round(hhi(lang_counter), 3),
        "year": round(hhi(year_counter), 3),
        "genre": round(hhi(genre_counter), 3),
    }

    # 排序逻辑（位置 vs 年份 / 位置 vs 热度 的斯皮尔曼相关）
    pos_years = [(i, s.publish_year) for i, s in enumerate(songs) if s.publish_year]
    pos_pops = [(i, s.popularity) for i, s in enumerate(songs) if s.popularity is not None]
    order = {}
    if len(pos_years) >= 3:
        corr_y = spearman([p for p, _ in pos_years], [y for _, y in pos_years])
        if corr_y >= 0.7: verdict = "按发行年份升序排列（时间线叙事）"
        elif corr_y <= -0.7: verdict = "按发行年份倒序排列（新歌在前）"
        elif corr_y >= 0.3: verdict = "年份整体偏旧歌在前，略有时间线感"
        elif corr_y <= -0.3: verdict = "年份整体偏新歌在前"
        else: verdict = "无明显按年份排序，多为混排"
        order["year"] = {"corr": round(corr_y, 3), "verdict": verdict}
    if len(pos_pops) >= 3:
        corr_p = spearman([p for p, _ in pos_pops], [q for _, q in pos_pops])
        if corr_p >= 0.7: verdict = "热度高的曲目靠前（按人气编排）"
        elif corr_p <= -0.7: verdict = "冷门曲目靠前，热门压轴"
        elif abs(corr_p) < 0.3: verdict = "顺序与热度无关，多为随机/手动编排"
        else: verdict = "热度与位置有轻度关联"
        order["popularity"] = {"corr": round(corr_p, 3), "verdict": verdict}

    # 合作检测
    collabs = []
    collab_count = 0
    for i, s in enumerate(songs):
        parts = extract_collabs(s.title, s.artists)
        if parts:
            collab_count += 1
            collabs.append({"title": s.title, "artists": s.artists, "partners": parts})
    collab_top = sorted(
        (p for c in collabs for p in c["partners"]),
        key=lambda x: artist_appearances.get(x, 0), reverse=True)[:8]
    collab_freq = Counter(p for c in collabs for p in c["partners"])

    # 口味画像
    profile = build_profile({
        "lang": lang_counter, "year": year_counter, "avg_year": round(statistics.mean(years)) if years else None,
        "avg_pop": round(statistics.mean(pops)) if pops else None,
        "top1_share": round(top1_track_share * 100, 1),
        "genre": genre_counter, "type": type_counter, "mood": mood_counter,
        "avg_duration": round(statistics.mean(durations)) if durations else 0,
        "lang_diversity": lang_diversity, "genre_diversity": genre_diversity,
        "conc": conc, "decade_count": len(decade_bins) if years else 0,
    }, pl)

    # 新鲜 / 怀旧 指数（两个独立、可解释的占比指标）
    cur_year = datetime.now().year
    fresh = nostalgia = nostalgic_share = None
    if years:
        fresh = round(sum(1 for y in years if y >= cur_year - 5) / len(years), 2)
        nostalgia = round(sum(1 for y in years if y < cur_year - 10) / len(years), 2)
        nostalgic_share = round(nostalgia * 100, 1)

    # 自动洞察
    insights = build_insights(n, durations, years, pops, lang_counter, genre_counter,
                              top_artists, lead_artist, top1_track_share, mood_counter,
                              album_counter, order, fresh, nostalgia)

    avg_pop = round(statistics.mean(pops)) if pops else None
    summary = build_summary(lang_counter, genre_counter, years, avg_pop, fresh,
                            mood_counter, profile, n)

    return {
        "total": n,
        "total_duration_ms": sum(durations),
        "avg_duration_ms": round(statistics.mean(durations)) if durations else 0,
        "min_duration_ms": min(durations) if durations else 0,
        "max_duration_ms": max(durations) if durations else 0,
        "total_duration_str": _fmt_duration(sum(durations)),
        "avg_duration_str": _fmt_short_duration(round(statistics.mean(durations))) if durations else "-",
        "duration_bins": duration_bins,
        "years": {"min": min(years) if years else None,
                  "max": max(years) if years else None,
                  "span": (max(years) - min(years)) if years else 0,
                  "decade_bins": decade_bins,
                  "year_counts": sorted(year_counter.items()),
                  "unknown": unknown_years,
                  "avg": round(statistics.mean(years)) if years else None},
        "artists": {"unique": len(artist_appearances),
                    "top": artist_stats,
                    "top1_track_share": round(top1_track_share * 100, 1)},
        "languages": {"rows": [{"name": k, "count": v, "pct": round(v / n * 100, 1)}
                                for k, v in lang_counter.most_common()],
                      "diversity": round(lang_diversity, 2)},
        "genres": {"rows": [{"name": k, "count": v, "pct": round(v / n * 100, 1)}
                             for k, v in genre_counter.most_common()],
                   "diversity": round(genre_diversity, 2)},
        "types": {"rows": [{"name": k, "count": v, "pct": round(v / n * 100, 1)}
                            for k, v in type_counter.most_common()]},
        "moods": {"rows": [{"name": k, "count": v, "pct": round(v / n * 100, 1)}
                            for k, v in mood_counter.most_common()],
                  "valence_raw": [v for _, v, _ in mood_tags],
                  "energy_raw": [e for _, _, e in mood_tags],
                  "valence_avg": round(statistics.mean([v for _, v, _ in mood_tags]), 2),
                  "energy_avg": round(statistics.mean([e for _, _, e in mood_tags]), 2)},
        "albums": {"unique": len(album_counter),
                   "top": [{"name": k, "count": v} for k, v in album_counter.most_common(8)]},
        "concentration": conc,
        "ordering": order,
        "cohorts": {"rows": [{"name": k, "count": v, "pct": round(v / n * 100, 1)}
                              for k, v in cohort_counter.most_common()],
                    "artists": dict(cohort_artist_counter)},
        "collabs": {"count": collab_count,
                    "pct": round(collab_count / n * 100, 1),
                    "top_partners": [{"name": k, "count": v}
                                     for k, v in collab_freq.most_common(8)]},
        "popularity": {"avg": avg_pop, "bins": pop_bins if pops else []},
        "profile": profile,
        "freshness": fresh,
        "nostalgia": nostalgia,
        "nostalgic_share": nostalgic_share,
        "summary": summary,
        "diversity": {"lang_entropy": round(entropy, 2),
                      "lang_diversity": round(lang_diversity, 2)},
        "insights": insights,
        "cloud": build_word_cloud(songs, pl.tags),
        "top_songs": [{
            "rank": i + 1,
            "title": s.title,
            "artists": " / ".join(s.artists) if s.artists else "未知歌手",
            "album": s.album or "-",
            "duration": _fmt_short_duration(s.duration_ms),
            "year": s.publish_year or "-",
            "popularity": s.popularity if s.popularity is not None else "-",
            "url": s.url,
        } for i, s in enumerate(songs[:30])],
    }


def build_profile(d: Dict[str, Any], pl: Playlist) -> List[str]:
    tags = []
    lang = d["lang"]
    if lang:
        top_lang, top_cnt = lang.most_common(1)[0]
        ratio = top_cnt / sum(lang.values())
        if ratio >= 0.7:
            tags.append({"华语": "华语向", "英语": "欧美向", "日语": "日系向",
                         "韩语": "韩流向", "俄语": "俄语向"}.get(top_lang, top_lang + "向"))
    y = d["avg_year"]
    if y is not None:
        if y < 2005: tags.append("怀旧经典系")
        elif y < 2015: tags.append("千禧怀旧系")
        else: tags.append("新歌追更系")
    p = d["avg_pop"]
    if p is not None:
        tags.append("高热度流行向" if p >= 60 else "小众私藏向" if p < 40 else "热度均衡型")
    if d["top1_share"] >= 25:
        tags.append("本命歌手控")
    g = d["genre"]
    if g:
        top_genre, top_cnt = g.most_common(1)[0]
        if top_cnt / sum(g.values()) >= 0.35 and top_genre not in ("流行/其他",):
            tags.append(top_genre + "控")
    t = d["type"]
    if t:
        top_type, top_cnt = t.most_common(1)[0]
        tr = top_cnt / sum(t.values())
        if top_type == "纯音乐" and tr >= 0.15: tags.append("纯音乐爱好者")
        elif top_type == "翻唱" and tr >= 0.10: tags.append("翻唱收藏家")
    avg_min = d["avg_duration"] / 60000
    if avg_min and avg_min < 3.3: tags.append("短歌快节奏")
    elif avg_min and avg_min > 5: tags.append("长曲沉浸派")
    if d["lang_diversity"] < 0.3: tags.append("语言口味专一")
    elif d["lang_diversity"] > 0.7: tags.append("语种涉猎广")
    if not tags:
        tags.append("多元混搭")
    if len(tags) > 6:
        tags = tags[:6]
    return tags


def build_insights(n, durations, years, pops, lang_counter, genre_counter,
                   top_artists, lead_artist, top1_track_share, mood_counter,
                   album_counter, order, fresh, nostalgia) -> List[str]:
    insights = []
    if n < 20:
        insights.append("这是一份小而精的歌单（{}首），适合精听反复品味。".format(n))
    elif n >= 100:
        insights.append("歌单体量较大（{}首），更像一个日常聆听合集。".format(n))

    if durations:
        avg_min = statistics.mean(durations) / 60000
        insights.append("平均单曲时长约 {:.1f} 分钟，完整听完需要 {}。".format(
            avg_min, _fmt_duration(sum(durations))))
        if sum(durations) / 3600000 > 3:
            insights.append("总时长超过 3 小时，适合通勤/学习/驾车等长时间陪伴场景。")

    if lang_counter:
        dom_lang, dom_cnt = lang_counter.most_common(1)[0]
        ratio = dom_cnt / n
        insights.append("语言构成以{}为主，占 {:.0%}。".format(dom_lang, ratio))

    if genre_counter:
        top_genre, g_cnt = genre_counter.most_common(1)[0]
        insights.append("曲风上{}占比最高（{}首），{}。".format(
            top_genre, g_cnt, "整体听感偏统一" if g_cnt / n > 0.4 else "口味较为多元"))

    if years:
        insights.append("歌曲年份跨度从 {} 到 {}，平均发行于 {}。".format(
            min(years), max(years), round(statistics.mean(years))))

    if top_artists:
        a = top_artists[0][0]
        insights.append("出现频次最高的歌手是{}，在{}首歌中出镜。".format(
            a, top_artists[0][1]))
        if top1_track_share > 0.25:
            insights.append("{} 占据了超过四分之一的首席歌手位，个人风格极强。".format(a))

    if mood_counter:
        top_mood, m_cnt = mood_counter.most_common(1)[0]
        insights.append("情绪主色调是「{}」，占比 {:.0%}。".format(top_mood, m_cnt / n))

    if album_counter:
        insights.append("收录了 {} 张不同专辑的曲目，平均每张专辑入选 {:.1f} 首。".format(
            len(album_counter), n / len(album_counter)))

    if order.get("year"):
        insights.append("曲目编排：{}。".format(order["year"]["verdict"]))
    if order.get("popularity"):
        insights.append("热度编排：{}。".format(order["popularity"]["verdict"]))

    if pops:
        avg = statistics.mean(pops)
        insights.append("曲目平均热度 {:.0f} 分（0-100），{}。".format(
            avg, "整体偏高，多为热门单曲" if avg >= 60 else "偏冷门与私藏向" if avg < 40 else "热度中等"))
    if fresh is not None:
        insights.append("近五年发行的歌曲占 {:.0%}，{}。".format(
            fresh, "整体偏新，紧跟当下潮流" if fresh >= 0.5 else "新歌不多，口味偏沉淀"))
    if nostalgia is not None and nostalgia >= 0.2:
        insights.append("发行超过 10 年的旧歌占 {:.0%}，怀旧成分{}。".format(
            nostalgia, "明显" if nostalgia >= 0.5 else "尚可"))
    return insights[:9]


def build_summary(lang_counter, genre_counter, years, avg_pop, fresh,
                  mood_counter, profile, n) -> str:
    parts = []
    if lang_counter:
        top_lang, cnt = lang_counter.most_common(1)[0]
        parts.append("一份以{}为主的歌单".format(top_lang))
    if genre_counter:
        top_genre, gcnt = genre_counter.most_common(1)[0]
        if top_genre != "流行/其他" or gcnt / n < 0.5:
            parts.append("{}氛围浓厚".format(top_genre))
    if years:
        parts.append("歌曲横跨 {} 年时光".format(max(years) - min(years)))
    if mood_counter:
        top_mood, mcnt = mood_counter.most_common(1)[0]
        if mcnt / n >= 0.2:
            parts.append("情绪落在「{}」".format(top_mood))
    if avg_pop is not None:
        parts.append("热度{}".format("偏高" if avg_pop >= 60 else "偏冷门" if avg_pop < 40 else "适中"))
    if fresh is not None:
        parts.append("{}".format("偏新歌向" if fresh >= 0.45 else "偏怀旧向"))
    base = "，".join(parts[:5])
    tag_str = " / ".join(profile[:3])
    return "{base}。画像标签：{tags}。".format(base=base or "一份内容丰富的歌单", tags=tag_str or "多元" )


def build_word_cloud(songs: List[Song], tags: List[str], top: int = 24) -> List[Dict[str, Any]]:
    freq: Counter = Counter()
    zh_stop = set("的是在了我有和就不人都一这中与为个也于出上他大把被在到从对着让吧呢吗呀没很又就还都正要去看过但子儿地她它们你他她我你们我们")
    en_stop = {"the", "a", "an", "of", "to", "in", "on", "and", "for", "with", "me",
               "you", "my", "your", "i", "we", "our", "is", "are", "it", "this"}
    all_text = " ".join(s.title for s in songs) + " " + " ".join(tags)

    for w in re.findall(r"[A-Za-z]{2,}", all_text.lower()):
        if w not in en_stop:
            freq[w] += 1
    for seg in re.findall(r"[\u4e00-\u9fff]+", all_text):
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg[0] in zh_stop or bg[1] in zh_stop:
                continue
            freq[bg] += 1
    items = [{"word": w, "count": c} for w, c in freq.most_common(top * 2) if c >= 2][:top]
    if not items:
        return []
    max_c = max(i["count"] for i in items)
    for it in items:
        it["size"] = 13 + round(it["count"] / max_c * 22)
        it["rot"] = [-6, 0, 6, 0, -4, 4][hash(it["word"]) % 6]
    return items


# --------------------------------------------------------------------------- #
# SVG 图表生成（E 维度）
# --------------------------------------------------------------------------- #

def _path_from_pts(xy: List[Tuple[float, float]]) -> str:
    if not xy:
        return ""
    d = "M {:.1f},{:.1f}".format(*xy[0])
    for x, y in xy[1:]:
        d += " L {:.1f},{:.1f}".format(x, y)
    return d


def radar_svg(items: List[Tuple[str, float]], w: int = 520, h: int = 440) -> str:
    cx, cy, R = w / 2, h / 2 - 20, 148
    m = len(items)
    if m < 3:
        return ""
    ang = 2 * math.pi / m
    start = -math.pi / 2

    def pt(val, i, rr):
        a = start + i * ang
        return cx + rr * math.cos(a), cy + rr * math.sin(a)

    grid = ""
    for g, alpha in ((0.25, .05), (0.5, .08), (0.75, .10), (1.0, .14)):
        pts = " ".join("{:.1f},{:.1f}".format(*pt(g, i, R * g)) for i in range(m))
        grid += '<polygon points="{}" fill="rgba(255,255,255,{:.2f})" stroke="rgba(255,255,255,.05)"/>'.format(pts, alpha)
    axes = ""
    for i in range(m):
        x1, y1 = pt(1.0, i, R)
        axes += '<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="rgba(255,255,255,.06)"/>'.format(cx, cy, x1, y1)
    scaled = [min(1.0, max(0.03, v)) for _, v in items]
    data_pts = " ".join("{:.1f},{:.1f}".format(*pt(scaled[i], i, R * scaled[i])) for i in range(m))
    dots = ""
    for i, v in enumerate(scaled):
        x, y = pt(v, i, R * v)
        dots += '<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="#fff" class="radar-dot" style="animation-delay:{:.2f}s"/>'.format(x, y, 0.5 + i * 0.06)
    labels = ""
    for i, (lab, _) in enumerate(items):
        a = start + i * ang
        lx, ly = cx + (R + 34) * math.cos(a), cy + (R + 34) * math.sin(a)
        anchor = "middle" if abs(math.cos(a)) < 0.3 else ("start" if math.cos(a) > 0 else "end")
        labels += '<text x="{:.1f}" y="{:.1f}" text-anchor="{}" fill="#b9b9c8" font-size="13">{}</text>'.format(lx, ly, anchor, lab)
    grad = ('<defs><linearGradient id="radarg" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0%" stop-color="#8b5cf6"/><stop offset="100%" stop-color="#22d3ee"/>'
            '</linearGradient></defs>')
    return ('<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" role="img">'
            '{}{}{}{}{}{}</svg>').format(
        w, h, grad, grid, axes,
        '<polygon points="{}" fill="url(#radarg)" fill-opacity=".30" stroke="url(#radarg)" stroke-width="2" class="radar-poly"/>'.format(data_pts),
        dots, labels)


def mood_curve_svg(valence: List[float], energy: List[float], w: int = 680, h: int = 260) -> str:
    n = len(valence)
    if n < 2:
        return ""
    v = _smooth(valence, max(3, n // 8))
    e = _smooth(energy, max(3, n // 8))
    pad_l, pad_r, pad_t, pad_b = 44, 16, 20, 38
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def scaled(vals):
        return [(pad_l + i / (n - 1) * iw,
                 pad_t + (1 - min(1.0, max(0.0, x))) * ih) for i, x in enumerate(vals)]

    vp, ep = scaled(v), scaled(e)
    area = '<polygon points="{} {},{}" fill="url(#moodArea)" opacity="0"/>'.format(
        " ".join("{:.1f},{:.1f}".format(x, y) for x, y in vp), pad_l + iw, pad_t + ih)
    ygrid = ""
    for g in (0.0, 0.5, 1.0):
        y = pad_t + (1 - g) * ih
        ygrid += '<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="rgba(255,255,255,.06)"/>'.format(pad_l, y, pad_l + iw, y)
    ylab = ""
    for g, t in ((0.0, "积极"), (0.5, "中性"), (1.0, "消极")):
        y = pad_t + (1 - g) * ih
        ylab += '<text x="4" y="{:.1f}" fill="#9a9aa8" font-size="11" text-anchor="start">{}</text>'.format(y + 3, t)
    grad = ('<defs>'
            '<linearGradient id="moodArea" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#8b5cf6" stop-opacity=".28"/>'
            '<stop offset="100%" stop-color="#22d3ee" stop-opacity=".02"/></linearGradient>'
            '</defs>')
    area = area.replace('opacity="0"', 'class="mood-area"')
    return ('<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" role="img">'
            '{}{}{}{}{}{}</svg>').format(
        w, h, grad, ygrid, ylab, area,
        '<path d="{}" pathLength="100" fill="none" stroke="#8b5cf6" stroke-width="2.6" class="draw"/>'.format(_path_from_pts(vp)),
        '<path d="{}" pathLength="100" fill="none" stroke="#22d3ee" stroke-width="2.6" class="draw" style="animation-delay:.45s"/>'.format(_path_from_pts(ep)))


def year_line_svg(year_counts: List[Tuple[int, int]], w: int = 680, h: int = 250) -> str:
    if len(year_counts) < 2:
        return ""
    xs = [y for y, _ in year_counts]
    ys = [c for _, c in year_counts]
    ymin, ymax = min(xs), max(xs)
    cmax = max(ys)
    pad_l, pad_r, pad_t, pad_b = 44, 16, 20, 40
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def px(y): return pad_l + (y - ymin) / (ymax - ymin) * iw
    def py(c): return pad_t + (1 - c / cmax) * ih

    pts = [(px(y), py(c)) for y, c in year_counts]
    area = '<polygon points="{} {} {},{}" fill="url(#yearArea)" class="mood-area"/>'.format(
        pad_l, pad_t + ih, " ".join("{:.1f},{:.1f}".format(x, y) for x, y in pts), pad_l + iw, pad_t + ih)
    xtick = ""
    step = max(1, (ymax - ymin) // 5)
    for y in range(ymin, ymax + 1, step):
        xtick += '<text x="{:.1f}" y="{}" fill="#9a9aa8" font-size="11" text-anchor="middle">{}</text>'.format(px(y), h - 10, y)
    ygrid = ""
    for c in (0, cmax):
        y = py(c)
        ygrid += '<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="rgba(255,255,255,.06)"/>'.format(pad_l, y, pad_l + iw, y)
    grad = ('<defs><linearGradient id="yearArea" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#22d3ee" stop-opacity=".30"/>'
            '<stop offset="100%" stop-color="#8b5cf6" stop-opacity=".02"/></linearGradient></defs>')
    bars = ""
    bw = max(6, iw / len(year_counts) * 0.5)
    for y, c in year_counts:
        hh = (py(0) - py(c))
        bars += '<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="3" fill="rgba(34,211,238,.16)"/>'.format(px(y) - bw / 2, py(c), bw, hh)
    return ('<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" role="img">'
            '{}{}{}{}{}{}</svg>').format(
        w, h, grad, ygrid, bars, area,
        '<path d="{}" pathLength="100" fill="none" stroke="#22d3ee" stroke-width="2.6" class="draw"/>'.format(_path_from_pts(pts)),
        xtick)


def collab_network_svg(network: Dict[str, Any], w: int = 680, h: int = 380) -> str:
    """合作网络力导向图（斥力+弹力布局）。nodes: name/weight；edges: a/b/count。"""
    nodes = network.get("nodes", [])
    edges = network.get("edges", [])
    if not nodes:
        return ""
    n = len(nodes)
    cx, cy = w / 2, h / 2
    R = min(w, h) / 2 - 78
    pos = {nd["name"]: [cx + R * math.cos(2 * math.pi * i / n),
                        cy + R * math.sin(2 * math.pi * i / n)]
           for i, nd in enumerate(nodes)}

    # 斥力 + 弹力迭代（更强斥力避免重叠，弱中心引力聚拢）
    for _ in range(200):
        disp = {k: [0.0, 0.0] for k in pos}
        keys = list(pos)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d2 = dx * dx + dy * dy
                if d2 < 1e-6:
                    d2 = 1e-6
                d = math.sqrt(d2)
                rep = 6500.0 / d2
                ux, uy = dx / d, dy / d
                disp[a][0] += ux * rep
                disp[a][1] += uy * rep
                disp[b][0] -= ux * rep
                disp[b][1] -= uy * rep
        for e in edges:
            if e["a"] in pos and e["b"] in pos:
                dx = pos[e["b"]][0] - pos[e["a"]][0]
                dy = pos[e["b"]][1] - pos[e["a"]][1]
                d = math.sqrt(dx * dx + dy * dy) or 1e-6
                atr = e["count"] * 0.15
                ux, uy = dx / d, dy / d
                disp[e["a"]][0] += ux * atr
                disp[e["a"]][1] += uy * atr
                disp[e["b"]][0] -= ux * atr
                disp[e["b"]][1] -= uy * atr
        for k in pos:
            # 弱中心引力
            dx = cx - pos[k][0]
            dy = cy - pos[k][1]
            dd = math.sqrt(dx * dx + dy * dy) or 1e-6
            grav = 0.008 * dd
            disp[k][0] += dx / dd * grav
            disp[k][1] += dy / dd * grav
            pos[k][0] += disp[k][0]
            pos[k][1] += disp[k][1]
    for k in pos:
        pos[k][0] = min(w - 46, max(46, pos[k][0]))
        pos[k][1] = min(h - 36, max(30, pos[k][1]))

    wmax = max((nd["weight"] for nd in nodes), default=1)
    cmax = max((e["count"] for e in edges), default=1)
    # 颜色按权重从橙到深棕渐变
    def node_color(w):
        t = w / wmax
        r = 217 - int(120 * t)
        g = 108 + int(40 * t)
        b = 47 + int(20 * t)
        return "rgb({},{},{})".format(r, g, b)

    lines = ""
    for e in edges:
        if e["a"] in pos and e["b"] in pos:
            op = 0.15 + 0.6 * (e["count"] / cmax)
            sw = 0.8 + 2.2 * (e["count"] / cmax)
            lines += '<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="#d96c2f" stroke-width="{:.1f}" stroke-opacity="{:.2f}" data-a="{}" data-b="{}"/>'.format(
                pos[e["a"]][0], pos[e["a"]][1], pos[e["b"]][0], pos[e["b"]][1], sw, op,
                e["a"], e["b"])

    circles = ""
    for nd in nodes:
        r = 8 + 15 * (nd["weight"] / wmax)
        c = node_color(nd["weight"])
        circles += ('<g class="net-node" data-name="{}">'
                    '<circle cx="{:.1f}" cy="{:.1f}" r="{:.1f}" fill="{}" stroke="#fff" stroke-width="1.5"/>'
                    '<text x="{:.1f}" y="{:.1f}" text-anchor="middle" fill="#22201c" font-size="10.5" dy="3.5" font-weight="600">{}</text>'
                    '</g>').format(
            nd["name"], pos[nd["name"]][0], pos[nd["name"]][1], r, c,
            pos[nd["name"]][0], pos[nd["name"]][1], _clip_text(nd["name"], 8 if r < 14 else 10))
    return '<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" role="img" data-net>{}{}</svg>'.format(w, h, lines, circles)


def genre_year_heatmap(gxy: Dict[str, Any], w: int = 680, h: int = 320) -> str:
    """曲风×年代热力图。rows: genre/decade/count。"""
    decades = gxy.get("decades", [])
    rows = gxy.get("rows", [])
    if not decades or not rows:
        return ""
    genres = []
    seen = set()
    for r in rows:
        if r["genre"] not in seen:
            genres.append(r["genre"])
            seen.add(r["genre"])
    genres = genres[:12]
    cmax = max(r["count"] for r in rows) or 1
    pad_l, pad_r, pad_t, pad_b = 96, 14, 14, 34
    gw = (w - pad_l - pad_r) / len(decades)
    gh = (h - pad_t - pad_b) / len(genres)
    cells = ""
    idx = {g: i for i, g in enumerate(genres)}
    for r in rows:
        if r["genre"] not in idx or r["decade"] not in decades:
            continue
        gi = idx[r["genre"]]
        di = decades.index(r["decade"])
        a = 0.06 + 0.85 * (r["count"] / cmax)
        x = pad_l + di * gw + 2
        y = pad_t + gi * gh + 2
        cells += '<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="4" fill="#d96c2f" fill-opacity="{:.2f}" data-cnt="{}" class="hm-cell"/>'.format(
            x, y, gw - 4, gh - 4, a, r["count"])
    ylab = ""
    for i, g in enumerate(genres):
        ylab += '<text x="{}" y="{:.1f}" text-anchor="end" fill="#666" font-size="11" dy="3.5">{}</text>'.format(pad_l - 8, pad_t + i * gh + gh / 2, _clip_text(g, 9))
    xlab = ""
    for i, d in enumerate(decades):
        xlab += '<text x="{:.1f}" y="{}" text-anchor="middle" fill="#666" font-size="11">{}</text>'.format(pad_l + i * gw + gw / 2, h - 10, "{}s".format(str(d)[:4]))
    return '<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" role="img">{}{}{}</svg>'.format(w, h, ylab, cells, xlab)


def _clip_text(s: str, max_chars: int) -> str:
    s = s or ""
    return s if len(s) <= max_chars else s[:max_chars - 1] + "…"


# --------------------------------------------------------------------------- #
# Markdown 报告
# --------------------------------------------------------------------------- #

def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([line, sep] + body)


def render_markdown(pl: Playlist, a: Dict[str, Any], fetched_at: str) -> str:
    n = a["total"]
    lines = []
    lines.append("# {} 歌单分析".format(pl.name))
    lines.append("")
    lines.append("> 平台：{} ｜ 分析时间：{}".format(PLATFORM_NAMES.get(pl.platform, pl.platform), fetched_at))
    lines.append("")

    lines.append("## 一句话结论")
    lines.append(a["summary"])
    lines.append("")

    lines.append("## 口味画像")
    lines.append("`{}`".format("`  `".join(a["profile"])))
    lines.append("")
    if a["freshness"] is not None:
        lines.append("- 近五年发行歌曲：**{:.0%}** ｜ 发行超十年旧歌：**{:.0%}**".format(
            a["freshness"], a["nostalgia"]))
        lines.append("")

    lines.append("## 基本信息")
    meta = [["歌单名称", pl.name], ["创建者", pl.creator or "-"],
            ["播放量", _fmt_count(pl.play_count) if pl.play_count else "-"],
            ["曲目数", str(n)], ["标签", "、".join(pl.tags) if pl.tags else "-"],
            ["总时长", a["total_duration_str"]], ["平均单曲", a["avg_duration_str"]]]
    if pl.created_time:
        try:
            meta.append(["创建时间", datetime.fromtimestamp(pl.created_time / 1000).strftime("%Y-%m-%d")])
        except (ValueError, OSError):
            pass
    lines.append(_md_table(["项目", "内容"], meta))
    lines.append("")
    if pl.description:
        lines.append("**简介**：{}".format(pl.description))
        lines.append("")

    lines.append("## 时长维度")
    lines.append(_md_table(["区间", "曲目数", "占比"], _pct_table(a["duration_bins"], n)))
    lines.append("")
    lines.append("- 最短单曲：{} ｜ 最长单曲：{}".format(
        _fmt_short_duration(a["min_duration_ms"]), _fmt_short_duration(a["max_duration_ms"])))
    lines.append("")

    if a["years"]["min"] is not None:
        lines.append("## 年代维度")
        lines.append(_md_table(["年代", "曲目数", "占比"], _pct_table(
            [("{}年代".format(b[0]), b[1]) for b in a["years"]["decade_bins"]], n, fmt="{:.1f}%")))
        lines.append("")
        if a["years"]["unknown"]:
            lines.append("> {} 首歌曲年份未知。".format(a["years"]["unknown"]))
            lines.append("")
    else:
        lines.append("## 年代维度")
        lines.append("该平台未返回歌曲发行年份，无法进行年代分析。")
        lines.append("")

    lines.append("## 歌手维度")
    lines.append("- 共出现 **{}** 位不同歌手/艺人 ｜ 首席歌手占比最高 {}% 的曲目。".format(
        a["artists"]["unique"], a["artists"]["top1_track_share"]))
    lines.append("")
    lines.append(_md_table(["#", "歌手", "首席位", "总出镜", "占比"], [
        [str(i + 1), t["name"], str(t["lead"]), str(t["appear"]), "{}%".format(t["pct"])]
        for i, t in enumerate(a["artists"]["top"])]))
    lines.append("")

    if a["cohorts"]["rows"]:
        lines.append("### 歌手代际")
        lines.append(_md_table(["代际", "曲目数", "占比"], _pct_table(
            [(i["name"], i["count"]) for i in a["cohorts"]["rows"]], n)))
        lines.append("")

    if a["collabs"]["count"]:
        lines.append("### 合作曲目")
        lines.append("- 共 **{}** 首（{:.0%}）包含合作/合唱特征。".format(
            a["collabs"]["count"], a["collabs"]["pct"] / 100))
        if a["collabs"]["top_partners"]:
            lines.append("- 高频合作对象：{}".format(
                "、".join("{}({}次)".format(p["name"], p["count"])
                          for p in a["collabs"]["top_partners"][:5])))
        lines.append("")

    lines.append("## 曲风维度")
    if a.get("genre_overview"):
        lines.append("> {}".format(a["genre_overview"]))
        lines.append("")
    lines.append(_md_table(["曲风", "曲目数", "占比"], _pct_table(
        [(i["name"], i["count"]) for i in a["genres"]["rows"]], n)))
    lines.append("")

    lines.append("## 歌曲类型维度")
    lines.append(_md_table(["类型", "曲目数", "占比"], _pct_table(
        [(i["name"], i["count"]) for i in a["types"]["rows"]], n)))
    lines.append("")

    lines.append("## 情绪维度")
    if a.get("mood_overview"):
        lines.append("> {}".format(a["mood_overview"]))
        lines.append("")
    lines.append("- 平均情绪：**{:.2f}**（0 悲观 → 1 乐观）｜ 平均能量：**{:.2f}**（0 静 → 1 动）".format(
        a["moods"]["valence_avg"], a["moods"]["energy_avg"]))
    lines.append("")
    lines.append(_md_table(["情绪", "曲目数", "占比"], _pct_table(
        [(i["name"], i["count"]) for i in a["moods"]["rows"]], n)))
    lines.append("")

    lines.append("## 语言维度")
    lines.append(_md_table(["语言", "曲目数", "占比"], _pct_table(
        [(i["name"], i["count"]) for i in a["languages"]["rows"]], n)))
    lines.append("")
    lines.append("- 语言多样性指数：{}（0 单一 → 1 多元）".format(a["languages"]["diversity"]))
    lines.append("")

    if a["popularity"]["avg"] is not None:
        lines.append("## 热度维度")
        lines.append("- 平均热度：**{}** / 100".format(a["popularity"]["avg"]))
        lines.append("")
        lines.append(_md_table(["热度区间", "曲目数", "占比"], _pct_table(a["popularity"]["bins"], n)))
        lines.append("")

    lines.append("## 专辑维度")
    if a["albums"]["top"]:
        lines.append("- 共收录 **{}** 张不同专辑的曲目。".format(a["albums"]["unique"]))
        lines.append("")
        lines.append(_md_table(["专辑", "入选曲目数"], [[t["name"], str(t["count"])] for t in a["albums"]["top"]]))
        lines.append("")
    else:
        lines.append("该平台未返回专辑信息。")
        lines.append("")

    lines.append("## 集中度指数（HHI）")
    lines.append("> HHI 取值 0（高度分散）→ 1（单一垄断）。")
    lines.append("")
    conc = a["concentration"]
    lines.append(_md_table(["维度", "HHI", "解读"], [
        ["歌手", str(conc["artist"]), "本命向" if conc["artist"] > 0.3 else "分散" if conc["artist"] < 0.15 else "中等"],
        ["专辑", str(conc["album"]), "专辑向" if conc["album"] > 0.3 else "单曲拼盘" if conc["album"] < 0.15 else "中等"],
        ["语言", str(conc["language"]), "语言专一" if conc["language"] > 0.5 else "语种多样" if conc["language"] < 0.3 else "中等"],
        ["曲风", str(conc["genre"]), "曲风专一" if conc["genre"] > 0.5 else "风格多样" if conc["genre"] < 0.3 else "中等"],
        ["年代", str(conc["year"]), "年代聚焦" if conc["year"] > 0.5 else "年代分散" if conc["year"] < 0.3 else "中等"],
    ]))
    lines.append("")

    if a["ordering"]:
        lines.append("## 排序逻辑")
        for k, v in a["ordering"].items():
            lines.append("- {}：{}（ρ={}）".format("按年份" if k == "year" else "按热度", v["verdict"], v["corr"]))
        lines.append("")

    lines.append("## 自动洞察")
    for ins in a["insights"]:
        lines.append("- " + ins)
    lines.append("")

    lines.append("## 精选曲目 Top 30")
    lines.append(_md_table(["#", "曲目", "歌手", "专辑", "时长", "年份", "热度"], [
        [str(t["rank"]), t["title"], t["artists"], t["album"], t["duration"], str(t["year"]), str(t["popularity"])]
        for t in a["top_songs"]]))
    lines.append("")

    lines.append("---")
    lines.append("报告由 playlist-analysis skill 自动生成。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #

def _load_template() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    tpl = os.path.join(here, "..", "templates", "report_template.html.j2")
    with open(os.path.normpath(tpl), encoding="utf-8") as f:
        return f.read()


def render_html(pl: Playlist, a: Dict[str, Any], fetched_at: str) -> str:
    tpl = _load_template()
    env = jinja2.Environment(autoescape=False)
    env.filters["fmt_duration"] = _fmt_short_duration
    env.filters["fmt_count"] = _fmt_count
    max_dur = max(a["duration_bins"], key=lambda b: b[1])[1] or 1
    max_dec = max([b[1] for b in a["years"]["decade_bins"]], default=1) or 1
    max_pop = max([b[1] for b in a["popularity"]["bins"]], default=1) or 1

    radar_items = [
        ("歌手多元", 1 - min(1.0, a["concentration"]["artist"] * 2)),
        ("专辑多元", 1 - min(1.0, a["concentration"]["album"] * 2)),
        ("语言多元", a["languages"]["diversity"]),
        ("曲风多元", a["genres"]["diversity"]),
        ("年代多元", 1 - min(1.0, a["concentration"]["year"] * 2)),
        ("情绪能量", a["moods"]["energy_avg"]),
    ]
    radar = radar_svg(radar_items)
    mood_svg = mood_curve_svg(a["moods"]["valence_raw"], a["moods"]["energy_raw"])
    year_svg = year_line_svg(a["years"]["year_counts"]) if a["years"]["year_counts"] else ""
    network_svg = collab_network_svg(a["collab_network"]) if a.get("collab_network") else ""
    heatmap_svg = genre_year_heatmap(a["genre_x_year"]) if a.get("genre_x_year") else ""

    ctx = {
        "pl": pl,
        "platform_name": PLATFORM_NAMES.get(pl.platform, pl.platform),
        "a": a,
        "fetched_at": fetched_at,
        "created_date": (datetime.fromtimestamp(pl.created_time / 1000).strftime("%Y-%m-%d")
                         if pl.created_time else ""),
        "duration_max": max_dur,
        "decade_max": max_dec,
        "pop_max": max_pop,
        "top_artists": a["artists"]["top"],
        "radar_svg": radar,
        "mood_svg": mood_svg,
        "year_svg": year_svg,
        "network_svg": network_svg,
        "heatmap_svg": heatmap_svg,
        "cloud_items": a["cloud"],
    }
    return env.from_string(tpl).render(**ctx)


# --------------------------------------------------------------------------- #
# 数据管道：切块导出 + AI 结果合并
# --------------------------------------------------------------------------- #

CHUNK_SIZE = 300
LYRIC_CHARS = 200  # 每首歌抓取的歌词截断长度

def fetch_lyrics(song: Song, headers: Dict[str, str]) -> str:
    """抓取网易云单曲歌词（含翻译），返回纯文本截断。失败返回空串。"""
    m = re.search(r"song\?id=(\d+)", song.url or "")
    if not m:
        return ""
    sid = m.group(1)
    try:
        r = requests.get("https://music.163.com/api/song/lyric",
                         params={"id": sid, "lv": 1, "kv": 1, "tv": -1},
                         headers=headers, timeout=TIMEOUT)
        d = r.json()
    except (ValueError, requests.exceptions.RequestException):
        return ""
    lrc = (d.get("lrc") or {}).get("lyric") or ""
    if not lrc:
        return ""
    lines = []
    for ln in lrc.splitlines():
        ln = re.sub(r"\[\d+:\d+\.\d+\]", "", ln).strip()
        if ln and not ln.startswith(("作词", "作曲", "编曲", "制作", "吉他", "和声",
                                     "混音", "母带", "录音", "监制", "OP", "SP")):
            lines.append(ln)
    text = " ".join(lines)
    if len(text) > LYRIC_CHARS:
        text = text[:LYRIC_CHARS] + "…"
    return text


def build_artist_manifest(pl: Playlist) -> List[Dict[str, Any]]:
    """导出歌手清单（频次/首席/代表曲目/年份区间），供子代理先聚合标注歌手风格。"""
    from collections import defaultdict
    info: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "appear": 0, "lead": 0, "titles": [], "years": []})
    for s in pl.songs:
        if not s.artists:
            continue
        lead = s.artists[0]
        for a in set(s.artists):
            info[a]["appear"] += 1
        info[lead]["lead"] += 1
        if s.title not in info[lead]["titles"]:
            info[lead]["titles"].append(s.title)
        if s.publish_year:
            info[lead]["years"].append(s.publish_year)
    rows = []
    for name, d in info.items():
        years = d["years"]
        rows.append({
            "name": name,
            "appear": d["appear"],
            "lead": d["lead"],
            "sample_titles": d["titles"][:4],
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None,
        })
    rows.sort(key=lambda x: x["appear"], reverse=True)
    return rows


def build_collab_edges(pl: Playlist) -> List[Dict[str, Any]]:
    """合作网络边：识别多歌手曲目与 feat，统计歌手对共现。"""
    edges: Dict[Tuple[str, str], int] = {}
    for s in pl.songs:
        artists = s.artists
        parts = list(artists)
        for p in extract_collabs(s.title, artists):
            if p not in parts:
                parts.append(p)
        parts = [p for p in parts if p]
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                key = tuple(sorted([parts[i], parts[j]]))
                edges[key] = edges.get(key, 0) + 1
    return [{"a": a, "b": b, "count": c} for (a, b), c in edges.items()]


def export_data(pl: Playlist, out_dir: str, with_lyrics: bool = False) -> Dict[str, str]:
    """浅层数据管道：导出全量歌曲、切块、基础统计，供 AI 主/子代理分析。"""
    os.makedirs(out_dir, exist_ok=True)
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/",
               "Cookie": "os=pc; appver=8.0.0; osver=win"}

    # 1. 全量歌曲（带全局 index，供子代理回填标注）
    songs_out = []
    for i, s in enumerate(pl.songs):
        row = {
            "index": i,
            "title": s.title,
            "artists": s.artists,
            "album": s.album,
            "duration_ms": s.duration_ms,
            "year": s.publish_year,
            "popularity": s.popularity,
            "url": s.url,
        }
        if with_lyrics:
            row["lyric"] = fetch_lyrics(s, headers)
        songs_out.append(row)
    playlist_path = os.path.join(out_dir, "playlist.json")
    with open(playlist_path, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "platform": pl.platform,
                "platform_name": PLATFORM_NAMES.get(pl.platform, pl.platform),
                "name": pl.name,
                "creator": pl.creator,
                "description": pl.description,
                "tags": pl.tags,
                "play_count": pl.play_count,
                "track_count": pl.track_count,
                "created_time": pl.created_time,
            },
            "songs": songs_out,
        }, f, ensure_ascii=False, indent=2)

    # 2. 切块（供子代理逐个分析，每块 CHUNK_SIZE 首）
    chunks_dir = os.path.join(out_dir, "chunks")
    os.makedirs(chunks_dir, exist_ok=True)
    chunk_paths = []
    for start in range(0, len(songs_out), CHUNK_SIZE):
        chunk = songs_out[start:start + CHUNK_SIZE]
        idx = start // CHUNK_SIZE
        cp = os.path.join(chunks_dir, "chunk_{:03d}.json".format(idx))
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"chunk": idx, "songs": chunk}, f, ensure_ascii=False, indent=2)
        chunk_paths.append(cp)

    # 3. 浅层统计（纯计算，不含曲风/情绪/类型/画像等语义字段）
    a = analyze(pl)
    stats = {
        "total": a["total"],
        "total_duration_str": a["total_duration_str"],
        "avg_duration_str": a["avg_duration_str"],
        "min_duration_ms": a["min_duration_ms"],
        "max_duration_ms": a["max_duration_ms"],
        "duration_bins": a["duration_bins"],
        "years": a["years"],
        "artists": a["artists"],
        "languages": a["languages"],
        "albums": a["albums"],
        "concentration": {k: v for k, v in a["concentration"].items() if k != "genre"},
        "ordering": a["ordering"],
        "cohorts": a["cohorts"],
        "collabs": a["collabs"],
        "popularity": a["popularity"],
        "freshness": a["freshness"],
        "nostalgia": a["nostalgia"],
        "nostalgic_share": a["nostalgic_share"],
        "diversity": a["diversity"],
        "cloud": a["cloud"],
        "top_songs": a["top_songs"],
    }
    stats_path = os.path.join(out_dir, "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # 4. 歌手清单（P0-1：先聚合标注歌手风格，再映射回歌曲）
    artist_rows = build_artist_manifest(pl)
    artists_path = os.path.join(out_dir, "artists.json")
    with open(artists_path, "w", encoding="utf-8") as f:
        json.dump({"artists": artist_rows}, f, ensure_ascii=False, indent=2)

    # 5. 合作网络边（P1-6：歌手×歌手共现）
    collab_edges = build_collab_edges(pl)
    network_path = os.path.join(out_dir, "collab_network.json")
    with open(network_path, "w", encoding="utf-8") as f:
        json.dump({"edges": collab_edges}, f, ensure_ascii=False, indent=2)

    # 6. 基准数据（P2-10：内置参考，供 AI 洞察对比）
    benchmark = {
        "netcase_hot100": {"avg_pop": 85, "avg_year": 2020,
                           "note": "网易云热歌榜参考"},
        "generic": {"avg_pop": 60, "avg_year": 2015,
                    "note": "平台歌单一般水平参考"},
    }
    benchmark_path = os.path.join(out_dir, "benchmark.json")
    with open(benchmark_path, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, ensure_ascii=False, indent=2)

    # 7. 子代理输出目录约定
    ai_dir = os.path.join(out_dir, "ai")
    os.makedirs(ai_dir, exist_ok=True)
    with open(os.path.join(ai_dir, ".gitkeep"), "w", encoding="utf-8") as f:
        f.write("")

    return {"playlist": playlist_path, "stats": stats_path, "chunks": chunk_paths,
            "artists": artists_path, "network": network_path,
            "benchmark": benchmark_path, "ai": ai_dir}


def load_ai_result(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_ai(a: Dict[str, Any], ai: Dict[str, Any]) -> Dict[str, Any]:
    """用 AI 汇总结果覆盖脚本的语义字段（曲风/情绪/类型/画像/结论/洞察）。"""
    if not ai:
        return a
    a = dict(a)
    for key in ("genres", "types", "moods", "scenes", "profile", "summary", "insights",
                "redundancy", "genre_x_year", "cloud", "collab_network", "top_songs"):
        if ai.get(key):
            a[key] = ai[key]
    if ai.get("genre_overview"):
        a["genre_overview"] = ai["genre_overview"]
    if ai.get("mood_overview"):
        a["mood_overview"] = ai["mood_overview"]
    return a


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="多平台歌单多维度分析")
    parser.add_argument("url", help="歌单链接，例如 https://music.163.com/#/playlist?id=xxx")
    parser.add_argument("-o", "--out", default=None, help="输出目录（默认：当前目录下的 playlist_report）")
    parser.add_argument("--only-md", action="store_true", help="只生成 Markdown 报告")
    parser.add_argument("--only-html", action="store_true", help="只生成 HTML 报告")
    parser.add_argument("--data-only", action="store_true",
                        help="只导出数据（playlist.json / chunks / stats.json），不做语义分析，供 AI 子代理分析")
    parser.add_argument("--ai-result", default=None, metavar="PATH",
                        help="读取 AI 汇总结果 ai_analysis.json，覆盖曲风/情绪/类型/画像/结论后渲染报告")
    parser.add_argument("--lyrics", action="store_true",
                        help="data-only 模式下额外抓取网易云歌词（每首截断 200 字）供子代理判情绪/主题")
    args = parser.parse_args()

    url = args.url.strip()
    platform = detect_platform(url)
    if not platform:
        print("无法识别平台，支持的平台：网易云、QQ音乐、酷狗、酷我。")
        return 1
    pid = extract_id(platform, url)
    if not pid:
        print("未能从链接中解析出歌单 ID：{}".format(url))
        return 1

    print("平台：{} ｜ ID：{}".format(PLATFORM_NAMES.get(platform, platform), pid))
    print("正在抓取歌单...")

    out_dir = args.out or os.path.join(os.getcwd(), "playlist_report")
    os.makedirs(out_dir, exist_ok=True)

    # AI 结果模式：优先复用 data-only 导出的 playlist.json，保证 index 与 AI 标注一致
    data_playlist = os.path.join(out_dir, "playlist.json")
    if args.ai_result and os.path.exists(data_playlist):
        try:
            pl = load_playlist_from_data(data_playlist)
            print("复用已导出的数据：{}（{} 首）".format(data_playlist, len(pl.songs)))
        except (ValueError, KeyError, OSError) as e:
            print("读取已导出数据失败（{}），改为在线抓取".format(e))
            pl = None
    else:
        pl = None

    if pl is None:
        try:
            pl = FETCHERS[platform](pid)
        except requests.exceptions.RequestException as e:
            print("抓取失败（网络/接口错误）：{}".format(e))
            return 1
        except (ValueError, KeyError, IndexError) as e:
            print("抓取失败（解析错误）：{}".format(e))
            return 1

    if not pl.songs:
        print("歌单为空或所有曲目解析失败。")
        return 1

    print("待分析 {} 首歌曲...".format(len(pl.songs)))
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 数据管道模式：只导出数据，不做语义分析（交给 AI 子代理）
    if args.data_only:
        paths = export_data(pl, out_dir, with_lyrics=args.lyrics)
        print("数据管道完成！已生成：")
        print("  - {}".format(paths["playlist"]))
        print("  - {}".format(paths["stats"]))
        print("  - {}".format(paths["artists"]))
        print("  - {}".format(paths["network"]))
        print("  - {}".format(paths["benchmark"]))
        print("  切块 {} 份（每份 ≤{} 首），供 AI 子代理逐个分析：".format(
            len(paths["chunks"]), CHUNK_SIZE))
        for cp in paths["chunks"]:
            print("    - {}".format(cp))
        print("  子代理标注请写入 {} 目录下 chunk_NNN.json".format(paths["ai"]))
        print("\n下一步：AI 汇总各块标注为 ai_analysis.json 后，用 --ai-result 渲染报告。")
        return 0

    a = analyze(pl)

    # AI 结果模式：用子代理汇总结果覆盖语义字段后渲染
    if args.ai_result:
        try:
            ai = load_ai_result(args.ai_result)
            a = merge_ai(a, ai)
            print("已合并 AI 分析结果：{}".format(args.ai_result))
        except (ValueError, OSError) as e:
            print("读取 AI 结果失败：{}（忽略，使用脚本默认分析）".format(e))

    written = []
    if not args.only_html:
        md_path = os.path.join(out_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(pl, a, fetched_at))
        written.append(md_path)
    if not args.only_md:
        html_path = os.path.join(out_dir, "report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(pl, a, fetched_at))
        written.append(html_path)

    json_path = os.path.join(out_dir, "analysis.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"playlist": pl.to_dict(), "analysis": a,
                   "fetched_at": fetched_at}, f, ensure_ascii=False, indent=2)
    written.append(json_path)

    print("完成！已生成：")
    for w in written:
        print("  - {}".format(w))

    print("\n画像：{}".format(" / ".join(a["profile"])))
    print("结论：{}".format(a["summary"]))
    print("\n要点速览：")
    for ins in a["insights"][:6]:
        print("  · " + ins)
    return 0


if __name__ == "__main__":
    sys.exit(main())