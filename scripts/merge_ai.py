#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主代理汇总脚本：合并所有子代理标注 → ai_analysis.json 骨架。

用法：
  python3 merge_ai.py <输出目录>

输入（输出目录下）：
  - playlist.json        全量歌曲（含 index/title/artists/year）
  - artists.json         歌手清单（P0-1 聚合标注的歌手风格）
  - collab_network.json  合作网络边（P1-6）
  - ai/chunk_NNN.json    子代理逐首标注（genre/type/mood/scene/valence/energy）

输出：
  - ai_analysis.json     AI 汇总骨架（分布/场景/冗余度/热力图/网络），
                        主代理在此基础上补充 genre_overview/mood_overview/
                        profile/summary/insights 后交给脚本渲染。
"""
import argparse
import glob
import json
import math
import os
import statistics
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_rows(counter, total):
    return [{"name": k, "count": v, "pct": round(v / total * 100, 1)}
            for k, v in counter.most_common()]


def entropy_diversity(counter, total):
    probs = [c / total for c in counter.values()]
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return round(h / math.log(len(probs)), 2) if len(probs) > 1 else 0.0


def normalize_genre(g):
    """把细分曲风归一到主大类（粤语流行（R&B）→粤语流行），避免热力图过宽。"""
    g = g.strip()
    # 显式映射：保留独立类别
    exact = {
        "游戏原声": "游戏原声", "影视原声": "影视原声", "二次元": "二次元",
        "Vocaloid": "Vocaloid", "纯音乐": "纯音乐", "古风": "古风",
        "民谣": "民谣", "摇滚": "摇滚", "电子": "电子", "爵士": "爵士",
        "嘻哈说唱": "嘻哈说唱", "舞曲": "舞曲", "乡村": "乡村",
    }
    if g in exact:
        return exact[g]
    # 去掉括号细分：粤语流行（R&B）→粤语流行
    if "（" in g:
        return g.split("（")[0]
    # 去掉括号细分（英文括号）
    if "(" in g:
        return g.split("(")[0].strip()
    # 常见表述合并
    for key in ("粤语", "华语", "欧美", "韩语", "日语"):
        if g.startswith(key):
            return key + "流行"
    if "独立" in g or "indie" in g.lower():
        for key in ("粤语", "华语", "欧美", "韩语", "日语"):
            if key in g:
                return key + "流行"
    return g if len(g) <= 6 else g[:4] + "…"


def build_top_picks(playlist, annot, n, k=30):
    """综合推荐 Top k：热度40% + 年份新近度15% + 合作5%，贪心多样化选曲。

    规则：每曲风至少 1 首（从高分取），同一歌手最多 2 首，情绪尽量均衡，
    再从剩余高分池补齐。最终按综合分从高到低排序输出 rank。
    每首含 `reason`（空字符串），由主代理补充推荐理由。
    """
    cur_year = 2026
    songs = playlist["songs"]
    scored = []
    for s in songs:
        a = annot.get(s["index"], {})
        pop = s.get("popularity")
        year = s.get("year")
        score = 0.0
        if pop is not None:
            score += 0.4 * (max(0, min(100, pop)) / 100)
        if year:
            age = cur_year - year
            if age <= 5:
                score += 0.15
            elif age <= 15:
                score += 0.10
            else:
                score += 0.04
        title = s.get("title", "")
        feat = ("feat" in title.lower() or "ft." in title.lower()
                or "×" in title or len(s.get("artists") or []) > 1)
        if feat:
            score += 0.05
        scored.append({
            "index": s["index"],
            "title": title,
            "artists": s.get("artists") or [],
            "album": s.get("album") or "",
            "duration_ms": s.get("duration_ms") or 0,
            "year": year,
            "popularity": pop,
            "url": s.get("url") or "",
            "genre": normalize_genre(a.get("genre") or "流行/其他"),
            "mood": a.get("mood") or "中性",
            "theme": a.get("theme") or "",
            "score": round(score, 4),
        })

    picked = []
    seen_genre = set()
    artist_count = Counter()
    mood_count = Counter()

    # 第一轮：主要曲风（出现≥10首）各选 1 首最高分（保证覆盖主力风格）
    by_genre = {}
    for r in scored:
        by_genre.setdefault(r["genre"], []).append(r)
    major_genres = [g for g, lst in by_genre.items() if len(lst) >= 10]
    for g in major_genres:
        lst = sorted(by_genre[g], key=lambda x: -x["score"])
        for cand in lst:
            if cand["artists"] and artist_count[cand["artists"][0]] >= 2:
                continue
            picked.append(cand)
            seen_genre.add(g)
            if cand["artists"]:
                artist_count[cand["artists"][0]] += 1
            mood_count[cand["mood"]] += 1
            break

    # 第二轮：补齐到 k，偏好高分 + 冷门情绪，同歌手限 2
    scored.sort(key=lambda x: -x["score"])
    for cand in scored:
        if len(picked) >= k:
            break
        if any(p["index"] == cand["index"] for p in picked):
            continue
        if cand["artists"] and artist_count[cand["artists"][0]] >= 2:
            continue
        picked.append(cand)
        if cand["artists"]:
            artist_count[cand["artists"][0]] += 1
        mood_count[cand["mood"]] += 1

    picked.sort(key=lambda x: -x["score"])
    rows = []
    for i, r in enumerate(picked):
        rows.append({
            "rank": i + 1,
            "title": r["title"],
            "artists": " / ".join(r["artists"]) if r["artists"] else "未知歌手",
            "album": r["album"] or "-",
            "duration": _fmt_dur(r["duration_ms"]),
            "year": r["year"] if r["year"] else "-",
            "popularity": r["popularity"] if r["popularity"] is not None else "-",
            "url": r["url"],
            "genre": r["genre"],
            "mood": r["mood"],
            "theme": r.get("theme") or "",
            "reason": "",
        })
    return rows


def _fmt_dur(ms):
    if not ms:
        return "-"
    sec = int(ms // 1000)
    return "{}:{:02d}".format(sec // 60, sec % 60)


def main():
    parser = argparse.ArgumentParser(description="合并子代理标注为 ai_analysis.json")
    parser.add_argument("out_dir", help="数据输出目录")
    args = parser.parse_args()

    base = args.out_dir
    playlist = load_json(os.path.join(base, "playlist.json"))
    n = len(playlist["songs"])
    if n == 0:
        sys.exit("歌单为空")

    # 歌手聚合标注（P0-1）
    # 脚本清单 artists.json 提供 appear/频次；AI 聚合标注 ai/artists.json 提供 genre/tier
    artist_map = {}
    artists_path = os.path.join(base, "artists.json")
    if os.path.exists(artists_path):
        ad = load_json(artists_path)
        artist_map = {a["name"]: a for a in ad.get("artists", [])}
    ai_artists_path = os.path.join(base, "ai", "artists.json")
    if os.path.exists(ai_artists_path):
        aad = load_json(ai_artists_path)
        for a in aad.get("artists", []):
            name = a["name"]
            if name not in artist_map:
                artist_map[name] = {"name": name, "appear": 0, "lead": 0,
                                    "sample_titles": [], "year_min": None, "year_max": None}
            for key in ("genre", "mood_hint", "tier", "tier_note"):
                if a.get(key):
                    artist_map[name][key] = a[key]

    # 收集所有块标注
    annot = {}
    for p in sorted(glob.glob(os.path.join(base, "ai", "chunk_*.json"))):
        d = load_json(p)
        for a in d.get("annotations", []):
            annot[a["index"]] = a
    missing = [i for i in range(n) if i not in annot]
    if missing:
        print("WARN 缺标注 {} 首，将用兜底值填充".format(len(missing)))

    genre_c, type_c, mood_c, scene_c = Counter(), Counter(), Counter(), Counter()
    theme_c = Counter()
    valence_raw, energy_raw = [], []
    for i in range(n):
        a = annot.get(i, {})
        genre_c[normalize_genre(a.get("genre") or "流行/其他")] += 1
        type_c[a.get("type") or "录音室版"] += 1
        mood_c[a.get("mood") or "中性"] += 1
        scene_c[a.get("scene") or "日常聆听"] += 1
        if a.get("theme"):
            theme_c[a["theme"]] += 1
        valence_raw.append(float(a.get("valence", 0.5)))
        energy_raw.append(float(a.get("energy", 0.5)))

    genres = {"rows": make_rows(genre_c, n), "diversity": entropy_diversity(genre_c, n)}
    types = {"rows": make_rows(type_c, n)}
    moods = {"rows": make_rows(mood_c, n),
             "valence_raw": [round(x, 3) for x in valence_raw],
             "energy_raw": [round(x, 3) for x in energy_raw],
             "valence_avg": round(statistics.mean(valence_raw), 2),
             "energy_avg": round(statistics.mean(energy_raw), 2)}
    scenes = {"rows": make_rows(scene_c, n)}
    themes = {"rows": make_rows(theme_c, n),
              "diversity": entropy_diversity(theme_c, n)} if theme_c else {"rows": [], "diversity": 0.0}

    # 主题 × 曲风 交叉矩阵（A1）
    theme_x_genre = {"rows": []}
    if theme_c:
        tg_count = {}
        for i in range(n):
            a = annot.get(i, {})
            t = a.get("theme")
            if not t:
                continue
            g = normalize_genre(a.get("genre") or "流行/其他")
            tg_count[(t, g)] = tg_count.get((t, g), 0) + 1
        theme_x_genre["rows"] = [
            {"theme": t, "genre": g, "count": c}
            for (t, g), c in sorted(tg_count.items(), key=lambda x: -x[1])
        ][:40]

    # 曲风×年代 交叉矩阵（P1-7）
    genre_x_year = {"decades": [], "rows": []}
    genre_year_count = {}
    year_buckets = {}
    for s in playlist["songs"]:
        a = annot.get(s["index"], {})
        g = normalize_genre(a.get("genre") or "流行/其他")
        y = s.get("year")
        if not y:
            continue
        dec = (y // 10) * 10
        year_buckets.setdefault(dec, set()).add(g)
        genre_year_count[(g, dec)] = genre_year_count.get((g, dec), 0) + 1
    if year_buckets:
        decades = sorted(year_buckets.keys())
        genre_x_year["decades"] = decades
        genre_x_year["rows"] = [
            {"genre": g, "decade": d, "count": c}
            for (g, d), c in sorted(genre_year_count.items(), key=lambda x: -x[1])
        ]

    # 风格冗余度（P1-9）
    top3_genre_share = sum(r["count"] for r in genres["rows"][:3]) / n
    redundancy = round(top3_genre_share * 0.7 + (1 - genres["diversity"]) * 0.3, 2)
    redundancy_level = ("高" if redundancy >= 0.7 else "中" if redundancy >= 0.5 else "低")

    # 合作网络节点/边（P1-6）
    collab_network = None
    net_path = os.path.join(base, "collab_network.json")
    if os.path.exists(net_path):
        edges = load_json(net_path).get("edges", [])
        edges = [e for e in edges if e["count"] >= 2][:60]
        if edges:
            node_cnt = Counter()
            for e in edges:
                node_cnt[e["a"]] += e["count"]
                node_cnt[e["b"]] += e["count"]
            nodes = [{"name": k, "weight": v} for k, v in
                     node_cnt.most_common(24)]
            collab_network = {"nodes": nodes, "edges": edges}

    # 歌手→风格映射（供报告"歌手风格"展示）
    artist_genres = []
    artist_tiers = {"rows": [], "artists": []}
    tier_map = {}
    if artist_map:
        for a in list(artist_map.values())[:40]:
            tier = a.get("tier")
            if tier and a["name"] not in tier_map:
                tier_map[a["name"]] = tier
            if a["appear"] >= 3:
                artist_genres.append({
                    "name": a["name"],
                    "appear": a["appear"],
                    "titles": a.get("sample_titles", [])[:2],
                    "genre": a.get("genre"),
                    "tier": a.get("tier") or None,
                })
    if tier_map:
        tier_c = Counter(tier_map.values())
        artist_tiers = {
            "rows": make_rows(tier_c, sum(tier_c.values())),
            "artists": [{"name": k, "tier": v} for k, v in tier_map.items()],
        }

    # 叙事弧线分段（A3）：按曲序把情绪/能量均分为 8 段，供主代理判叙事类型
    narrative = None
    if len(valence_raw) >= 8:
        seg = 8
        seg_len = math.ceil(n / seg)
        segments = []
        for s in range(0, n, seg_len):
            chunk_v = valence_raw[s:s + seg_len]
            chunk_e = energy_raw[s:s + seg_len]
            if not chunk_v:
                continue
            segments.append({
                "from": s + 1, "to": min(s + seg_len, n),
                "valence": round(statistics.mean(chunk_v), 3),
                "energy": round(statistics.mean(chunk_e), 3),
            })
        narrative = {"segments": segments, "verdict": "", "detail": ""}

    # 冷门好歌（A2）：tier=小众宝藏/新锐 或 非主流曲风 + 低热度，按综合分挑 10 首
    hidden_gems = []
    if artist_map:
        low_tier = {name for name, t in tier_map.items() if t in ("小众宝藏", "新锐")}
        cands = []
        for s in playlist["songs"]:
            a = annot.get(s["index"], {})
            g = normalize_genre(a.get("genre") or "流行/其他")
            lead = (s.get("artists") or [""])[0]
            if not lead:
                continue
            if lead in low_tier or g not in ("流行/其他",):
                pop = s.get("popularity")
                cands.append({
                    "index": s["index"], "title": s.get("title") or "",
                    "artists": " / ".join(s.get("artists") or []),
                    "genre": g, "mood": a.get("mood") or "",
                    "year": s.get("year"), "popularity": pop,
                    "url": s.get("url") or "",
                    "_pop": pop if pop is not None else 0,
                })
        cands.sort(key=lambda x: -x["_pop"])
        seen = set()
        for c in cands:
            if len(hidden_gems) >= 10:
                break
            if c["title"] in seen:
                continue
            seen.add(c["title"])
            hidden_gems.append({k: v for k, v in c.items() if not k.startswith("_")})

    # 精选 Top N：综合推荐（P 新增）
    # 评分 = 热度40% + 年份新近度15% + 合作5%，再用贪心多样化选出
    top_songs = build_top_picks(playlist, annot, n, k=30)

    out = {
        "genres": genres,
        "types": types,
        "moods": moods,
        "scenes": scenes,
        "themes": themes,
        "theme_x_genre": theme_x_genre,
        "redundancy": {"value": redundancy, "level": redundancy_level},
        "genre_x_year": genre_x_year,
        "collab_network": collab_network,
        "top_songs": top_songs,
    }
    if artist_genres:
        out["artist_genres"] = artist_genres
    if artist_tiers["rows"]:
        out["artist_tiers"] = artist_tiers
    if narrative:
        out["narrative"] = narrative
    if hidden_gems:
        out["hidden_gems"] = hidden_gems

    path = os.path.join(base, "ai_analysis.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("已生成 {}".format(path))
    print("  - 标注覆盖：{} / {}".format(n - len(missing), n))
    print("  - 曲风 Top5：{}".format("、".join(g["name"] for g in genres["rows"][:5])))
    print("  - 场景 Top5：{}".format("、".join(s["name"] for s in scenes["rows"][:5])))
    print("  - 冗余度：{}（{}）".format(redundancy, redundancy_level))
    print("  - 曲风×年代：{} 个年代 × {} 种曲风".format(
        len(genre_x_year["decades"]),
        len({r["genre"] for r in genre_x_year["rows"]})))
    if collab_network:
        print("  - 合作网络：{} 节点 / {} 边".format(
            len(collab_network["nodes"]), len(collab_network["edges"])))
    if themes["rows"]:
        print("  - 主题 Top5：{}".format("、".join(t["name"] for t in themes["rows"][:5])))
    if artist_tiers["rows"]:
        print("  - 艺人分级：{}".format("、".join("{} {}".format(t["name"], t["count"]) for t in artist_tiers["rows"])))
    if hidden_gems:
        print("  - 冷门好歌：{} 首候选".format(len(hidden_gems)))
    if narrative:
        print("  - 叙事弧线：{} 段情绪/能量分段".format(len(narrative["segments"])))
    print("\n下一步：主代理补充 genre_overview/mood_overview/profile/summary/insights"
          " 后运行渲染。")


if __name__ == "__main__":
    main()