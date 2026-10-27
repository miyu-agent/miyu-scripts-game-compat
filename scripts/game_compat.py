#!/usr/bin/env python3
# Display name: Game Linux compatibility
# 显示名称：游戏 Linux 兼容性
# Description: Check how a game runs on Linux: ProtonDB rating and recent player reports, anti-cheat status from AreWeAntiCheatYet, and the caniplayonlinux verdict. Takes a Steam App ID or a game name.
# 描述：查一款游戏在 Linux 上能不能玩：ProtonDB 评级和最近的玩家报告、AreWeAntiCheatYet 的反作弊状态、caniplayonlinux 的结论；给 Steam App ID 或游戏名。
# Timeout: 60
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "query": {"type": "string", "description": "Steam App ID or game name. The English title is most reliable; a Chinese name is matched through Steam's Chinese store names."},
#   "max_reports": {"type": "integer", "description": "Recent ProtonDB reports to show, 0-20. Default 6; 0 gives the ratings alone."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."}
# }, "required": ["query"]}
"""Three sources, looked up in two steps.

1. The query becomes a Steam App ID plus the English title: an App ID through
   Steam's appdetails, an English name through ProtonDB's Algolia index, a
   Chinese name through Steam's store search in the schinese locale first.
   A match that does not look like the game asked for is reported, not used.
2. ProtonDB (summary + player reports), AreWeAntiCheatYet (games.json) and
   caniplayonlinux (game page) are fetched at the same time with that identity.

A source with no entry says so; a source that failed says it failed. The two
are never mixed up: "not listed" is not "no anti-cheat".
"""

import datetime as dt
import html
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROTONDB = "https://www.protondb.com"
# ProtonDB's own search box queries this index with a public search-only key.
ALGOLIA_URL = "https://94he6yatei-dsn.algolia.net/1/indexes/steamdb/query"
ALGOLIA_HEADERS = {"x-algolia-application-id": "94HE6YATEI",
                   "x-algolia-api-key": "9ba0e69fb2974316cdaec8f5f257088f",
                   "Referer": PROTONDB}
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
AWACY_JSON = "https://raw.githubusercontent.com/AreWeAntiCheatYet/AreWeAntiCheatYet/HEAD/games.json"
CIPOL = "https://caniplayonlinux.com"

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
TIMEOUT = 15
CACHE_TTL = 24 * 3600
DEFAULT_REPORTS = 6
MAX_REPORTS = 20
NOTE_CAP = 300
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
FAULTS = [("audioFaults", "声音"), ("graphicalFaults", "画面"), ("windowingFaults", "窗口"),
          ("inputFaults", "输入"), ("saveGameFaults", "存档"), ("performanceFaults", "性能"),
          ("stabilityFaults", "稳定性"), ("significantBugs", "严重 bug")]
VERDICTS = {"recommended": "推荐", "not_recommended": "不推荐", "broken": "打不开", "unknown": "没写结论"}


class Failed(Exception):
    pass


class HttpError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- HTTP and cache

def fetch(url, body=None, headers=None, limit=8 << 20):
    hdrs = {"User-Agent": UA, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs)
    host = urllib.parse.urlsplit(url).hostname
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise HttpError(f"{host} 回了 HTTP {exc.code}", exc.code) from None
    except urllib.error.URLError as exc:
        raise HttpError(f"连不上 {host}：{exc.reason}") from None
    except (TimeoutError, socket.timeout):
        raise HttpError(f"{host} {TIMEOUT} 秒没有回应") from None
    if len(raw) > limit:
        raise HttpError(f"{host} 回的内容太大")
    return raw


def fetch_json(url, **kw):
    raw = fetch(url, **kw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError(f"{urllib.parse.urlsplit(url).hostname} 回的不是 JSON") from None


def cached(name, url):
    """Fetch through a 24-hour file cache in MIYU_SCRIPT_CACHE_DIR; a cache that cannot be written is skipped."""
    base = os.environ.get("MIYU_SCRIPT_CACHE_DIR")
    path = os.path.join(base, name) if base else None
    if path:
        try:
            if time.time() - os.path.getmtime(path) < CACHE_TTL:
                with open(path, "rb") as fh:
                    return fh.read()
        except OSError:
            pass
    raw = fetch(url)
    if path:
        try:
            os.makedirs(base, exist_ok=True)
            tmp = f"{path}.{os.getpid()}"
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, path)
        except OSError:
            pass
    return raw


# ---------------------------------------------------------------- text helpers

def flatten(value, cap=NOTE_CAP):
    """One line, capped, and no leading Markdown character that could break the list it is printed in."""
    text = " ".join(str(value).split())
    if len(text) > cap:
        text = text[:cap].rstrip() + "…"
    return "\\" + text if text[:1] in "#>-*+|=" else text


def norm(value):
    return "".join(ch for ch in value.lower() if ch.isalnum())


def month_day_year(value):
    """'Oct 31, 2024, 4:07 PM UTC', 'Fri, 26 Jan 2024 01:27 GMT', 'Thu Nov 17 2022 05:57' -> (year, month, day)."""
    parts = [p for p in re.split(r"[ ,]", value.lower()) if p]
    for i, part in enumerate(parts):
        month = next((n + 1 for n, m in enumerate(MONTHS) if part.startswith(m)), None)
        if month is None:
            continue
        around = [(parts[i + 1:i + 2], parts[i + 2:i + 3]), (parts[i - 1:i] if i else [], parts[i + 1:i + 2])]
        for day, year in around:
            if day and year and day[0].isdigit() and year[0].isdigit():
                d, y = int(day[0]), int(year[0])
                if 1 <= d <= 31 and 1970 <= y <= 2999:
                    return y, month, d
    return None


def iso_date(value):
    """Both date shapes the sources use -> YYYY-MM-DD; anything else passes through."""
    value = (value or "").strip()
    if re.match(r"\d{4}-\d\d-\d\d", value):
        return value[:10]
    parsed = month_day_year(value)
    return f"{parsed[0]:04d}-{parsed[1]:02d}-{parsed[2]:02d}" if parsed else value


# ---------------------------------------------------------------- step 1: which game

def algolia(query, hits=5):
    body = {"query": query, "facetFilters": [["appType:Game"]], "hitsPerPage": hits,
            "attributesToRetrieve": ["name", "objectID", "oslist"], "page": 0}
    found = fetch_json(ALGOLIA_URL, body=body, headers=ALGOLIA_HEADERS).get("hits")
    return [h for h in found or [] if isinstance(h, dict) and str(h.get("objectID", "")).isdigit()]


def steam_details(app_id, language="english"):
    """Store name and Linux build for an App ID; None when Steam has no such app."""
    value = fetch_json(f"{STEAM_DETAILS}?appids={app_id}&l={language}&filters=basic,platforms")
    entry = value.get(str(app_id)) or {}
    if not entry.get("success"):
        return None
    data = entry.get("data") or {}
    return {"name": data.get("name") or str(app_id), "linux": bool((data.get("platforms") or {}).get("linux"))}


def overlap(query, name):
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    other = set(w for w in re.split(r"[^a-z0-9]+", name.lower()) if w)
    return sum(w in other for w in words) / len(words) if words else 0.0


def identify(query):
    """{"app_id", "name", "linux"} for a game we trust is the one asked for;
    {"app_id": None, "miss": "..."} when the lookup found nothing that fits."""
    if query.isdigit():
        details = steam_details(int(query))
        if details is None:
            return {"app_id": None, "miss": f"Steam 上没有 App ID {query}"}
        return {"app_id": int(query), "name": details["name"], "linux": details["linux"]}

    if not CJK.search(query):
        hits = algolia(query)
        if not hits:
            return {"app_id": None, "miss": "Steam 上搜不到这个名字"}
        top = hits[0]
        name = str(top.get("name") or query)
        if overlap(query, name) < 0.5:
            # Algolia always returns something; for a game not on Steam it is an unrelated one ("Valorant" -> "NoFlash").
            others = "、".join(str(h.get("name")) for h in hits[:4])
            return {"app_id": None, "miss": f"Steam 上搜到的是 {others}，都不像这款游戏，它可能不在 Steam 上"}
        linux = "linux" in [str(o).lower() for o in top.get("oslist") or []]
        return {"app_id": int(top["objectID"]), "name": name, "linux": linux}

    # Chinese name: Steam's own Chinese titles first, then the Algolia index (which knows some Chinese aliases).
    wanted = norm(query)
    items = fetch_json(f"{STEAM_SEARCH}?term={urllib.parse.quote(query)}&l=schinese&cc=US").get("items") or []
    app_id = None
    for item in items:
        if norm(str(item.get("name", ""))) == wanted:
            app_id = int(item["id"])
            break
    candidates = [str(i.get("name")) for i in items[:4]]
    if app_id is None:
        for hit in algolia(query):
            name = str(hit.get("name") or "")
            if CJK.search(name):
                got = norm(name)
                if got == wanted or (wanted in got and len(wanted) >= 0.7 * len(got)):
                    app_id = int(hit["objectID"])
                    break
            elif name.isascii() and name:
                # An English title for a Chinese query: check it against the game's own Chinese store name.
                zh = steam_details(int(hit["objectID"]), "schinese")
                got = norm(zh["name"]) if zh else ""
                if got and (got == wanted or (wanted in got and len(wanted) >= 0.7 * len(got))):
                    app_id = int(hit["objectID"])
                    break
                name = f"{name}（{zh['name']}）" if zh else name
            candidates.append(name)
            break  # only the top hit is trusted; later ones are noise
    if app_id is None:
        seen = "、".join(dict.fromkeys(c for c in candidates if c)) or "没有"
        return {"app_id": None, "miss": f"Steam 上没对上这个中文名（搜到的：{seen}）。换英文原名再查；不在 Steam 上的游戏（WeGame、自家启动器）ProtonDB 本来就没有"}
    details = steam_details(app_id) or {"name": query, "linux": False}
    return {"app_id": app_id, "name": details["name"], "linux": details["linux"]}


# ---------------------------------------------------------------- ProtonDB

def protondb_report_id(app_id, reports, timestamp):
    """The reports file name, reverse-engineered from the ProtonDB frontend."""
    def r(e, t, n):
        return f"{t}p{(e * (t % n)) & 0xFFFFFFFFFFFFFFFF}"
    acc = 0
    for ch in f"p{r(app_id, reports, timestamp)}*vRT{r(1, app_id, timestamp)}undefined" + "m":
        acc = ((acc << 5) - acc + ord(ch)) & 0xFFFFFFFF
    return abs(acc - 0x100000000 if acc >= 0x80000000 else acc)


def one_report(raw):
    answers = raw.get("responses") if isinstance(raw.get("responses"), dict) else {}
    verdict = answers.get("verdictOob") if isinstance(answers.get("verdictOob"), str) else answers.get("verdict")
    if answers.get("startsPlay") != "yes":
        verdict = "broken"
    elif verdict == "yes":
        verdict = "recommended"
    elif isinstance(verdict, str):
        verdict = "not_recommended"
    else:
        verdict = "unknown"
    variant = answers.get("variant")
    proton = ("Proton Experimental" if variant == "experimental"
              else answers.get("customProtonVersion") if variant == "ge"
              else answers.get("protonVersion"))
    notes = answers.get("notes") if isinstance(answers.get("notes"), dict) else {}
    note = notes.get("concludingNotes") or answers.get("concludingNotes")
    stamp = raw.get("timestamp")
    try:
        date = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        date = "日期不明"
    return {"date": date, "verdict": verdict, "proton": proton if isinstance(proton, str) else None,
            "launch": flatten(answers["launchOptions"]) if isinstance(answers.get("launchOptions"), str) and answers["launchOptions"].strip() else None,
            "faults": [label for key, label in FAULTS if answers.get(key) == "yes"],
            "note": flatten(note) if isinstance(note, str) and note.strip() else None}


def protondb(app_id, max_reports):
    """None when ProtonDB has no page for the game."""
    try:
        summary = fetch_json(f"{PROTONDB}/api/v1/reports/summaries/{app_id}.json")
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    data = {"tier": summary.get("tier"), "trending": summary.get("trendingTier"), "best": summary.get("bestReportedTier"),
            "total": summary.get("total") if isinstance(summary.get("total"), int) else 0,
            "reports": [], "reports_error": None}
    if max_reports:
        try:
            counts = fetch_json(f"{PROTONDB}/data/counts.json")
            pid = protondb_report_id(app_id, int(counts["reports"]), int(counts["timestamp"]))
            raw = fetch_json(f"{PROTONDB}/data/reports/all-devices/app/{pid}.json")
            items = raw.get("reports") if isinstance(raw.get("reports"), list) else []
            data["reports"] = [one_report(i) for i in items[:max_reports] if isinstance(i, dict)]
        except (HttpError, KeyError, TypeError, ValueError) as exc:
            data["reports_error"] = str(exc) or type(exc).__name__  # a failed fetch must not read as "no reports"
    return data


# ---------------------------------------------------------------- AreWeAntiCheatYet

def awacy(app_id, name):
    """Steam App ID first, then the exact name. None means not listed, which says nothing about anti-cheat."""
    try:
        games = json.loads(cached("awacy-games.json", AWACY_JSON).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError("AreWeAntiCheatYet 的 games.json 解析不了") from None
    games = [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []
    if app_id is not None:
        for game in games:
            if str((game.get("storeIds") or {}).get("steam", "")) == str(app_id):
                return game
    wanted = norm(name)
    for game in games:
        if wanted and norm(str(game.get("name", ""))) == wanted:
            return game
    return None


# ---------------------------------------------------------------- caniplayonlinux

def slugify(name):
    """Apostrophes and ampersands are dropped, everything else non-alphanumeric is a dash: Baldur's Gate 3 -> baldurs-gate-3."""
    name = re.sub(r"[’'&＆™®]", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def cipol_similar(a, b):
    if a == b:
        return 100
    long_, short = (a, b) if len(a) >= len(b) else (b, a)
    if short and short in long_:
        return 70 + 30 * len(short) // len(long_)
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return 100 * common // len(long_) if long_ else 0


def cipol(name):
    """None when the site has no page for the game."""
    slug = slugify(name)
    if not slug:  # a purely non-Latin name: /games// is the catalogue front page
        return None
    url = f"{CIPOL}/games/{slug}/"
    try:
        page = fetch(url, limit=5 << 20).decode("utf-8", "replace")
    except HttpError as exc:
        if exc.status != 404:
            raise
        sitemap = cached("caniplayonlinux-sitemap.xml", f"{CIPOL}/sitemap-0.xml").decode("utf-8", "replace")
        best, url = 0, None
        for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sitemap):
            if not loc.startswith(f"{CIPOL}/games/"):
                continue
            candidate = loc[len(CIPOL) + 7:].strip("/")
            if candidate and not candidate.isdigit():
                score = cipol_similar(slug, candidate)
                if score >= 75 and score > best:
                    best, url = score, loc
        if url is None:
            return None
        page = fetch(url, limit=5 << 20).decode("utf-8", "replace")
    summary = None
    for pattern in (r'<meta name="description" content="([^"]*)"', r'<meta property="og:description" content="([^"]*)"'):
        found = re.search(pattern, page)
        if found and found.group(1).strip():
            summary = html.unescape(found.group(1).strip())
            break
    text = html.unescape(re.sub(r"(?s)<(script|style|svg)\b.*?</\1\s*>|<[^>]+>", "\n", page))
    lines = [line.strip(" :") for line in text.splitlines() if line.strip(" :")]
    verified = None
    for i, line in enumerate(lines[:-1]):
        if line.lower() == "last verified" and re.search(r"(19|20)\d\d", lines[i + 1]) and len(lines[i + 1]) <= 40:
            verified = iso_date(lines[i + 1])
            break
    return {"summary": summary, "verified": verified, "url": url}


# ---------------------------------------------------------------- output

def attempt(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as exc:  # noqa: BLE001 - every source failure is shown, not raised
        return "err", str(exc) or type(exc).__name__


def render(query, ident, proton, cheat, site):
    out = []
    if ident[0] == "err":
        out += [f"# {query}", f"认游戏这一步出错了：{ident[1]}"]
    elif ident[1]["app_id"] is None:
        out += [f"# {query}", f"⚠ {ident[1]['miss']}"]
    else:
        game = ident[1]
        out.append(f"# {game['name']}")
        out.append(f"Steam App ID {game['app_id']}" + (" · 有原生 Linux 版" if game["linux"] else ""))

    if proton is not None:
        if proton[0] == "err":
            out.append(f"\n## ProtonDB：出错\n{proton[1]}")
        elif proton[1] is None:
            out.append("\n## ProtonDB：没有这款游戏的报告")
        else:
            data = proton[1]
            tier = data["tier"] or "未评级"
            bits = [tier]
            if data["trending"] and data["trending"] != tier:
                bits.append(f"最近 {data['trending']}")
            if data["best"] and data["best"] != tier:
                bits.append(f"最好 {data['best']}")
            bits.append(f"{data['total']} 份报告")
            out.append(f"\n## ProtonDB：{' · '.join(bits)}")
            out.append(f"{PROTONDB}/app/{ident[1]['app_id']}")
            if data["reports_error"]:
                out.append(f"玩家报告没取到：{data['reports_error']}")
            if data["reports"]:
                out.append("最近的报告：")
                for rep in data["reports"]:
                    head = f"- {rep['date']} {VERDICTS[rep['verdict']]} · {rep['proton'] or 'Proton 版本没写'}"
                    if rep["faults"]:
                        head += f" · 有问题：{'、'.join(rep['faults'])}"
                    out.append(head)
                    if rep["launch"]:
                        out.append(f"  启动参数：{rep['launch']}")
                    if rep["note"]:
                        out.append(f"  {rep['note']}")

    if cheat[0] == "err":
        out.append(f"\n## 反作弊（AreWeAntiCheatYet）：出错\n{cheat[1]}")
    elif cheat[1] is None:
        out.append("\n## 反作弊（AreWeAntiCheatYet）：没收录（不等于没有反作弊）")
    else:
        game = cheat[1]
        anticheats = "、".join(a for a in game.get("anticheats") or [] if isinstance(a, str)) or "没列"
        out.append(f"\n## 反作弊（AreWeAntiCheatYet）：{game.get('status', '状态不明')} · {anticheats}")
        out.append(f"{iso_date(str(game.get('dateChanged') or ''))} 更新 · https://areweanticheatyet.com/game/{game.get('slug', '')}")
        updates = [u for u in game.get("updates") or [] if isinstance(u, dict)]
        updates.sort(key=lambda u: iso_date(str(u.get("date") or "")), reverse=True)
        for update in updates[:2]:
            out.append(f"- {iso_date(str(update.get('date') or ''))} {flatten(update.get('name') or '')}")
        for note in (game.get("notes") or [])[:3]:
            if isinstance(note, list) and note and isinstance(note[0], str):
                out.append(f"- {flatten(note[0])}")

    if site[0] == "err":
        out.append(f"\n## caniplayonlinux：出错\n{site[1]}")
    elif site[1] is None:
        out.append("\n## caniplayonlinux：没有这款游戏的页面")
    else:
        entry = site[1]
        out.append("\n## caniplayonlinux" + (f"：{entry['verified']} 核实" if entry["verified"] else ""))
        if entry["summary"]:
            out.append(flatten(entry["summary"], 600))
        out.append(entry["url"])
    return "\n".join(out).strip()


def check(query, max_reports):
    ident = attempt(identify, query)
    found = ident[0] == "ok" and ident[1]["app_id"] is not None
    # A query we could not pin to a Steam game still has a chance on the anti-cheat list by name (Valorant, LoL).
    name = ident[1]["name"] if found else query
    app_id = ident[1]["app_id"] if found else None
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_proton = pool.submit(attempt, protondb, app_id, max_reports) if found else None
        f_cheat = pool.submit(attempt, awacy, app_id, name)
        f_site = pool.submit(attempt, cipol, name)
        return query, ident, f_proton.result() if f_proton else None, f_cheat.result(), f_site.result()


def plain(text):
    """flatten() guards Markdown with a leading backslash; JSON wants the text itself."""
    return text[1:] if isinstance(text, str) and text.startswith("\\") else text


def source_json(result, build):
    if result is None:
        return {"status": "skipped"}
    if result[0] == "err":
        return {"status": "error", "error": result[1]}
    if result[1] is None:
        return {"status": "not_listed"}
    return dict({"status": "ok"}, **build(result[1]))


def to_json(query, ident, proton, cheat, site):
    out = {"ok": True, "query": query, "game": None}
    if ident[0] == "err":
        out["game_error"] = ident[1]
    elif ident[1]["app_id"] is None:
        out["game_error"] = ident[1]["miss"]
    else:
        game = ident[1]
        out["game"] = {"app_id": game["app_id"], "name": game["name"], "linux_native": game["linux"]}
    out["protondb"] = source_json(proton, lambda d: {
        "tier": d["tier"], "trending_tier": d["trending"], "best_tier": d["best"], "total_reports": d["total"],
        "url": f"{PROTONDB}/app/{ident[1]['app_id']}", "reports_error": d["reports_error"],
        "reports": [dict(r, launch=plain(r["launch"]), note=plain(r["note"])) for r in d["reports"]]})

    def anticheat(game):
        updates = [u for u in game.get("updates") or [] if isinstance(u, dict)]
        updates.sort(key=lambda u: iso_date(str(u.get("date") or "")), reverse=True)
        return {"rating": game.get("status"),
                "anticheats": [a for a in game.get("anticheats") or [] if isinstance(a, str)],
                "updated": iso_date(str(game.get("dateChanged") or "")) or None,
                "url": f"https://areweanticheatyet.com/game/{game.get('slug', '')}",
                "updates": [{"date": iso_date(str(u.get("date") or "")), "name": u.get("name") or ""} for u in updates],
                "notes": [n[0] for n in game.get("notes") or [] if isinstance(n, list) and n and isinstance(n[0], str)]}
    out["anticheat"] = source_json(cheat, anticheat)
    out["anticheat"]["source"] = "AreWeAntiCheatYet"
    out["caniplayonlinux"] = source_json(site, lambda e: {"verified": e["verified"], "summary": e["summary"],
                                                          "url": e["url"]})
    return out


def known_params():
    """开头 Parameters 里写的参数名：不认识的参数直接报错，免得写错的名字被悄悄当成没给。"""
    lines, inside = [], False
    with open(__file__, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            if line.startswith("# Parameters:"):
                inside = True
            elif inside:
                lines.append(line[1:])
    return set(json.loads("".join(lines)).get("properties", {}))


def reject_unknown(args):
    unknown = sorted(set(args) - known_params())
    if unknown:
        raise Failed(f"不认识的参数：{'、'.join(unknown)}。能用的：{'、'.join(sorted(known_params()))}")


def main():
    as_json = False
    try:
        try:
            args = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise Failed(f"参数不是 JSON：{exc}")
        if not isinstance(args, dict):
            raise Failed("参数要是一个 JSON 对象")
        as_json = args.get("format") == "json"
        reject_unknown(args)
        if args.get("format") not in (None, "md", "json"):
            raise Failed(f"format 只认 md、json，不认 {args['format']}")
        query = str(args.get("query") or "").strip()
        if not query:
            raise Failed("没给 query：写 Steam App ID 或游戏名")
        reports = args.get("max_reports", DEFAULT_REPORTS)
        if isinstance(reports, bool) or not isinstance(reports, (int, float)) or reports < 0:
            raise Failed("max_reports 要是 0–20 的整数")
        parts = check(query, min(int(reports), MAX_REPORTS))
        print(json.dumps(to_json(*parts), ensure_ascii=False) if as_json else render(*parts))
    except Failed as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) if as_json else f"错误：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
