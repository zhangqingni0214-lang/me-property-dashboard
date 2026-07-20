# -*- coding: utf-8 -*-
"""中东房产 · 账号&房源数据看板 —— 数据同步脚本。

复用『中东房产全链路看板』项目的 VidFlow 客户端与凭证，产出看板用的 data.js。

数据来源与口径：
  · 账号范围：内容类别(businessDirectionName) 同时含「迪拜」和「房」的账号（部门 4/6）。
  · 视频环节：VidFlow /api/ownAccount/video/daily/query，按账号ID聚合。
  · 私信环节：VidFlow /api/task/im/list，按账号聚合（累积值）。
  · 线索环节：CRM /api/leads/full-export，按来源账号(source_account→@handle)聚合
             新增/合格/约面（阶段组累积）。密钥从环境变量 CRM_FULL_KEY 读，仅聚合计数入 data.js。
  · 房源维度：视频标题里的房源码(DXB-xxxx / AS-/VR-/AR-/CR-...) → 房源平台直查
             GET /api/public/listings/<码> → 拿到该房源的 类别(期房/现房)/小区名称/户型，
             按此归因。期房按「社区名」聚合，现房按「小区名+户型(型号)」聚合。

用法：  python3 sync_data.py [发布窗口天数，默认30]
输出：  ./data.js  （window.DASHBOARD_DATA = {...}；看板 <script src> 直接读，免 CORS）
"""
from __future__ import annotations

import os
import re
import sys
import json
import asyncio
import urllib.request
import urllib.parse
from datetime import date, timedelta, datetime
from pathlib import Path

# —— 复用现有管线（VidFlow 客户端 + 凭证 + musewen token）——
# 优先用脚本同目录的副本（launchd 自动任务跑在 ~/claude专用，Desktop 受 macOS TCC 限制），
# 否则回退到桌面的开发主本。
_HERE = Path(__file__).resolve().parent
if (_HERE / "vidflow_client.py").exists():
    PIPELINE_DIR = _HERE
else:
    PIPELINE_DIR = Path("/Users/a58/Desktop/Claude本地/中东房产全链路看板")
sys.path.insert(0, str(PIPELINE_DIR))
from vidflow_client import VidFlowClient          # noqa: E402
from config import PROP_API_BASE, PROP_API_TOKEN  # noqa: E402


# 本地 .env 自动加载（无依赖）：CRM_FULL_KEY 等敏感项放此文件，勿提交/公开。
def _load_dotenv(p: Path):
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(Path(__file__).parent / ".env")

# 房源库正式专用 token（2026-07-17 发放）优先；未配置时回退全链路项目的旧只读 token
PROP_API_TOKEN = os.getenv("PROP_LISTINGS_TOKEN", "").strip() or PROP_API_TOKEN

HERE = Path(__file__).parent
OUT_JS = HERE / "data.js"
OUT_JSON = HERE / "data.json"
WINDOW_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

# musewen 主机根（PROP_API_BASE 末段是 /listings）
PROP_ROOT = PROP_API_BASE.rsplit("/", 1)[0]        # .../api/public
LISTINGS_URL = f"{PROP_ROOT}/listings"
OFFPLAN_COMM_URL = f"{PROP_ROOT}/offplan/communities"

# 视频数值字段（与 全链路看板/main.py 口径一致）
VID_FIELDS = {
    "totalVV": "累计播放量",
    "likes": "累计点赞量",
    "comments": "累计评论量",
    "shares": "累计分享量",
}

# ── CRM 线索（OK Leads）────────────────────────────────────────────────
# 密钥只从环境变量读，不硬编码、不写入 data.js（data.js 只放聚合计数，不含敏感字段）。
CRM_BASE = os.getenv("CRM_API_BASE", "https://leads.okteamcal.com").rstrip("/")
CRM_FULL_KEY = os.getenv("CRM_FULL_KEY", "").strip()
# 阶段组漏斗（按顺序累积）：新增=全部线索；合格=进入 Qualified 及以后；约面=进入 Viewing 及以后。
LEAD_QUALIFIED_GROUPS = {"Qualified", "Viewing", "Deal"}
LEAD_VIEWING_GROUPS = {"Viewing", "Deal"}
# 线索对象里"来源账号""阶段组"的候选字段名（实测后据此取，兼容多种命名）
LEAD_ACCOUNT_KEYS = ("source_account", "sourceAccount", "account", "来源账号")
LEAD_GROUP_KEYS = ("group", "stage_group", "stageGroup", "stage", "status", "阶段组", "状态")

# 视频标题里的房源码：DXB-6D0E18 / AS-176186 / VR-173340 / AR-.. / CR-.. 等
# 这些码在房源平台是有效房源ID，可直查 GET /api/public/listings/<码> 拿到房源明细。
PROP_CODE_RE = re.compile(r"\b([A-Z]{2,4}-[A-Z0-9]{4,8})\b")

# ── 期房图谱（开发商-社区-项目）────────────────────────────────────────
# 期房社区映射以 /api/public/offplan-graph 为准：平台期房小区名 → 图谱项目 → 6大社区。
# 仅期房做社区上卷；现房按房源ID展示、不映射社区。
OFFPLAN_GRAPH_URL = f"{PROP_ROOT}/offplan-graph"
OFFPLAN_GRAPH_TOKEN = os.getenv("OFFPLAN_GRAPH_TOKEN", "").strip()


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_offplan_graph():
    """拉图谱 → (映射函数, 6大社区名列表)。拉取失败则不上卷（期房保留原小区名）。"""
    canonical: list = []
    exact: dict = {}
    fuzzy: list = []
    try:
        req = urllib.request.Request(OFFPLAN_GRAPH_URL,
                                     headers={"X-Read-Token": OFFPLAN_GRAPH_TOKEN})
        with urllib.request.urlopen(req, timeout=30) as r:
            g = json.load(r)
        comm_name = {c["communityCode"]: c["name"]["en"] for c in g.get("communities", [])}
        canonical = sorted(comm_name.values())
        for c in g.get("communities", []):
            exact[_norm_name(c["name"]["en"])] = comm_name[c["communityCode"]]
        for p in g.get("projects", []):
            code = p.get("communityCode")
            if not code or code not in comm_name:
                continue
            pn = _norm_name(p["name"]["en"])
            if pn:
                exact.setdefault(pn, comm_name[code])
                if len(pn) >= 8:
                    fuzzy.append((pn, comm_name[code]))
        print(f"[图谱] 社区 {len(canonical)} · 项目匹配词 {len(exact)}")
    except Exception as e:
        print(f"[图谱] 拉取失败({e})，期房不上卷、保留原小区名")

    def map_offplan(comm: str) -> str:
        """期房小区名 → 6大社区名（映射不到则原样返回）。"""
        n = _norm_name(comm)
        if not n:
            return comm
        if n in exact:
            return exact[n]
        for pn, cname in fuzzy:
            if pn in n or n in pn:
                return cname
        return comm

    return map_offplan, canonical


def _num(v) -> float:
    try:
        if isinstance(v, str):
            v = v.replace(",", "").replace("%", "").strip()
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


def _prop_get(url: str, params: dict | None = None) -> dict:
    """musewen 只读接口 GET（中文参数需 urlencode，否则服务器 400）。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Read-Token": PROP_API_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _is_dubai_property(acct: dict) -> bool:
    b = acct.get("businessDirectionName") or ""
    return ("迪拜" in b) and ("房" in b)


# ── 房源平台：按房源码直查房源明细（后台数据关联）────────────────────────
_LISTING_CACHE: dict = {}


def resolve_code(code: str) -> dict | None:
    """GET /api/public/listings/<码> → 单条房源明细；命中返回 dict，否则 None（结果缓存）。"""
    if code in _LISTING_CACHE:
        return _LISTING_CACHE[code]
    url = f"{LISTINGS_URL}/{urllib.parse.quote(code)}"
    req = urllib.request.Request(url, headers={"X-Read-Token": PROP_API_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        d = d if (isinstance(d, dict) and d.get("房源ID")) else None
    except Exception:
        d = None
    _LISTING_CACHE[code] = d
    return d


def resolve_video_listing(title: str) -> dict | None:
    """从标题提取房源码，返回首个能查到的房源明细。"""
    for code in PROP_CODE_RE.findall(title or ""):
        d = resolve_code(code)
        if d:
            return d
    return None


# ── 视频聚合 ────────────────────────────────────────────────────────────
def _blank_video():
    return {"videos": 0, "totalVV": 0.0, "maxVV": 0.0,
            "likes": 0.0, "comments": 0.0, "shares": 0.0}


def _add_video(agg: dict, row: dict):
    agg["videos"] += 1
    vv = _num(row.get(VID_FIELDS["totalVV"]))
    agg["totalVV"] += vv
    agg["maxVV"] = max(agg["maxVV"], vv)
    agg["likes"] += _num(row.get(VID_FIELDS["likes"]))
    agg["comments"] += _num(row.get(VID_FIELDS["comments"]))
    agg["shares"] += _num(row.get(VID_FIELDS["shares"]))


def _blank_im():
    return {"dms": 0.0, "replies": 0.0, "whatsapp": 0.0}


def _blank_lead():
    return {"newLeads": 0, "qualified": 0, "meetings": 0}


def _lead_field(lead: dict, keys: tuple) -> str:
    for k in keys:
        v = lead.get(k)
        if v:
            return str(v)
    return ""


def fetch_leads(since: str, until: str) -> list:
    """CRM 全字段导出，按入库日(record_time)过滤 [since, until]，翻页取全。"""
    if not CRM_FULL_KEY:
        return []
    out, offset, limit = [], 0, 1000
    while True:
        params = {"since": since, "until": until, "date_field": "record",
                  "limit": limit, "offset": offset}
        url = CRM_BASE + "/api/leads/full-export?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-API-Key": CRM_FULL_KEY,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
        leads = d.get("leads") or []
        out.extend(leads)
        nxt = d.get("next_offset")
        if not leads or nxt is None or len(out) >= (d.get("total") or 0):
            break
        offset = nxt
    return out


def _norm_key(s: str) -> str:
    """规范化：小写 + 去非字母数字，用于把 CRM 业务简称对到账号昵称/handle。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _reached(lead: dict, groups: set) -> bool:
    """线索是否曾到达 groups 中任一阶段（用 _stageHist 累积，含当前 group）。"""
    stages = {h.get("stage") for h in (lead.get("_stageHist") or [])}
    stages.add(lead.get("group"))
    return bool(stages & groups)


def aggregate_leads(leads: list, accounts: list) -> dict:
    """按来源账号聚合 新增/合格/约面。source_account 是业务简称，
    用规范化后与账号 handle / nickname 匹配（相等或作为子串）。漏斗按 _stageHist 累积。"""
    norm_accts = []       # (handle, handle_norm, nick_norm)
    for a in accounts:
        h = a.get("username") or a.get("id") or ""
        nk = a.get("nickname") or a.get("account") or ""
        norm_accts.append((h, _norm_key(h), _norm_key(nk)))
    agg: dict = {}
    for ld in leads:
        sa = _norm_key(_lead_field(ld, LEAD_ACCOUNT_KEYS))
        if len(sa) < 3:
            continue
        handle = None
        for h, hn, nn in norm_accts:
            if sa in (hn, nn) or (len(sa) >= 4 and (sa in hn or sa in nn)):
                handle = h
                break
        if not handle:
            continue
        a = agg.setdefault(handle.lower(), _blank_lead())
        a["newLeads"] += 1                                    # 新增 = 归属该账号的全部线索
        if _reached(ld, LEAD_QUALIFIED_GROUPS):
            a["qualified"] += 1                               # 合格 = 曾到达 Qualified 及以后
        if _reached(ld, LEAD_VIEWING_GROUPS):
            a["meetings"] += 1                                # 约面 = 曾到达 Viewing 及以后
    return agg


def resolve_lead_listings(ld: dict) -> list:
    """从线索 listing_id 字段提取全部有效房源ID（字段可为纯ID或整段描述文本）。
    写了多个ID → 每个都归因（一条线索可多对应多个房源/社区）。
    返回 [(房源ID, 房源明细), ...]（去重、仅保留房源平台能查到的）。"""
    raw = _lead_field(ld, ("listing_id", "listingId", "房源ID"))
    out, seen = [], set()
    for code in PROP_CODE_RE.findall(raw.upper()):
        if code in seen:
            continue
        seen.add(code)
        d = resolve_code(code)
        if d:
            out.append((code, d))
    return out


def aggregate_leads_by_listing(leads: list, map_offplan=lambda c: c) -> tuple:
    """房源级线索归因：listing_id 形如房源ID → 房源平台解析。
    现房 → 按房源ID聚合（不映射社区）；期房 → 小区名经图谱上卷后按社区聚合。
    返回 (ready: {房源ID: {...}}, offplan: {社区名: {...}})。"""
    ready: dict = {}
    offplan: dict = {}
    for ld in leads:
        q = _reached(ld, LEAD_QUALIFIED_GROUPS)
        m = _reached(ld, LEAD_VIEWING_GROUPS)
        touched = set()   # 同一线索对同一目标行只计一次（两个ID同社区不重复计）
        for lid, d in resolve_lead_listings(ld):
            cat = (d.get("类别") or "").strip()
            comm = (d.get("小区名称") or "").strip()
            if cat == "现房":
                key = ("ready", lid)
                if key in touched:
                    continue
                g = ready.setdefault(lid, {**_blank_lead(),
                                           "community": comm,
                                           "huxing": (d.get("户型") or "").strip()})
            elif cat == "期房" and comm:
                comm = map_offplan(comm)
                key = ("offplan", comm)
                if key in touched:
                    continue
                g = offplan.setdefault(comm, {**_blank_lead(), "community": comm, "huxing": ""})
            else:
                continue
            touched.add(key)
            g["newLeads"] += 1
            if q:
                g["qualified"] += 1
            if m:
                g["meetings"] += 1
    return ready, offplan


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def leads_detail(leads: list, accounts: list, map_offplan=lambda c: c) -> tuple:
    """逐条线索明细（供前端按日期区间聚合）：
    {d:入库日, h:归属账号handle或空, lid:现房房源ID或空, q:曾达合格, m:曾达约面}。
    只保留能归到账号或现房房源的线索；不含任何客户敏感字段。"""
    norm_accts = []
    for a in accounts:
        h = a.get("username") or ""
        nk = a.get("nickname") or ""
        norm_accts.append((h, _norm_key(h), _norm_key(nk)))
    out, listing_meta = [], {}
    for ld in leads:
        rt = (ld.get("record_time") or "").strip()
        since = (ld.get("_since") or "").strip()
        dstr = rt[:10] if _ISO_DATE_RE.match(rt) else (since[:10] if _ISO_DATE_RE.match(since) else "")
        # 账号归属（同 aggregate_leads 的规范化匹配）
        handle = ""
        sa = _norm_key(_lead_field(ld, LEAD_ACCOUNT_KEYS))
        if len(sa) >= 3:
            for h, hn, nn in norm_accts:
                if sa in (hn, nn) or (len(sa) >= 4 and (sa in hn or sa in nn)):
                    handle = h
                    break
        # 房源归属：现房→房源ID(lid)；期房→社区名(oc)。
        # 字段可含多个ID → 每个目标各出一行（多对应）；账号(h)只挂在第一行，避免账号维度重复计数。
        q = 1 if _reached(ld, LEAD_QUALIFIED_GROUPS) else 0
        m = 1 if _reached(ld, LEAD_VIEWING_GROUPS) else 0
        targets = []      # [(lid, oc)]
        touched = set()
        for lid, d in resolve_lead_listings(ld):
            cat = (d.get("类别") or "").strip()
            if cat == "现房":
                if ("r", lid) in touched:
                    continue
                touched.add(("r", lid))
                if lid not in listing_meta:
                    listing_meta[lid] = {"lc": (d.get("小区名称") or "").strip(),
                                         "lx": (d.get("户型") or "").strip()}
                targets.append((lid, ""))
            elif cat == "期房":
                oc = map_offplan((d.get("小区名称") or "").strip())
                if not oc or ("o", oc) in touched:
                    continue
                touched.add(("o", oc))
                targets.append(("", oc))
        if not handle and not targets:
            continue
        if not targets:
            targets = [("", "")]      # 只归账号、无房源
        for i, (lid, oc) in enumerate(targets):
            out.append({"d": dstr, "h": handle if i == 0 else "",
                        "lid": lid, "oc": oc, "q": q, "m": m})
    return out, listing_meta


async def main():
    end = date.today()
    start = end - timedelta(days=WINDOW_DAYS - 1)
    print(f"=== 同步窗口：发布 {start} ~ {end}（{WINDOW_DAYS}天）===")

    client = VidFlowClient()

    # 1) 账号 + 过滤（迪拜 & 房）
    accts = await client.fetch_operating_accounts()
    targets = [a for a in accts if _is_dubai_property(a)]
    handles = {a.get("username") for a in targets if a.get("username")}
    print(f"[账号] 总 {len(accts)} → 迪拜房产类 {len(targets)}")

    # 2) 视频（仅目标账号）
    videos = await client.fetch_videos(start.isoformat(), end.isoformat(), handles)
    # VidFlow T+1：客户端已把统计日锚到「最新可用快照」，各视频行带 __statDate。
    snaps = sorted({r.get("__statDate") for r in videos if r.get("__statDate")})
    latest_snapshot = snaps[-1] if snaps else end.isoformat()
    print(f"[视频] 命中 {len(videos)} 条 · 最新快照日 {latest_snapshot}")

    vid_by_handle: dict = {}
    for r in videos:
        h = r.get("账号ID")
        if not h:
            continue
        _add_video(vid_by_handle.setdefault(h, _blank_video()), r)

    # 3) 私信（账号级累积）
    ims = await client.fetch_im()
    im_by_handle: dict = {}
    for t in ims:
        u = ((t.get("accountOperating") or {}).get("username"))
        if u not in handles:
            continue
        a = im_by_handle.setdefault(u, _blank_im())
        a["dms"] += _num(t.get("totalImCount"))
        a["replies"] += _num(t.get("successReplyCount"))
        a["whatsapp"] += _num(t.get("whatsappJoinCount"))
    print(f"[私信] 命中账号 {len(im_by_handle)}")

    # 3.5) 线索（CRM），按来源账号聚合
    leads = fetch_leads(start.isoformat(), end.isoformat())
    lead_by_handle = aggregate_leads(leads, targets)
    if CRM_FULL_KEY:
        print(f"[线索] 拉取 {len(leads)} 条 · 命中账号 {len(lead_by_handle)}")
    else:
        print("[线索] 未设置环境变量 CRM_FULL_KEY，跳过（线索置0）")

    # 4) 组装账号维度
    accounts_out = []
    for a in targets:
        h = a.get("username")
        v = vid_by_handle.get(h, _blank_video())
        im = im_by_handle.get(h, _blank_im())
        accounts_out.append({
            "id": h,
            "account": a.get("nickname") or h,
            "owner": a.get("ownerName") or "",
            "group": a.get("teamGroupName") or "",
            "category": a.get("businessDirectionName") or "",
            **{k: round(v[k]) for k in ("videos", "totalVV", "maxVV", "likes", "comments", "shares")},
            **{k: round(im[k]) for k in ("dms", "replies", "whatsapp")},
            **lead_by_handle.get((h or "").lower(), _blank_lead()),
        })
    accounts_out.sort(key=lambda x: x["totalVV"], reverse=True)

    # 5) 房源维度：视频标题房源码 → 房源平台直查 → 按 房源(期房社区/现房房源ID) 聚合
    #    期房：小区名 → 图谱上卷到 6 大社区（映射不到保留原名）；现房：按房源ID，不映射社区。
    map_offplan, canonical_comms = load_offplan_graph()
    groups: dict = {}          # key -> {type,name,community,agg}
    matched = no_code = unresolved = other_cat = 0
    for r in videos:
        title = r.get("视频标题") or ""
        if not PROP_CODE_RE.search(title):
            no_code += 1
            continue
        d = resolve_video_listing(title)
        if not d:
            unresolved += 1
            continue
        cat = (d.get("类别") or "").strip()
        # 口径：只取房源平台类别=期房/现房；租房、售房、商业地产等一律不进房源维度
        if cat not in ("期房", "现房"):
            other_cat += 1
            continue
        matched += 1
        comm = (d.get("小区名称") or "").strip() or "(未知小区)"
        huxing = (d.get("户型") or "").strip()
        lid = (d.get("房源ID") or "").strip()
        if cat == "期房":
            comm = map_offplan(comm)                      # 图谱上卷到 6 大社区
            key = ("期房", comm)
            name = comm                                   # 期房显示社区名
        else:                                             # 现房
            key = ("现房", lid or comm)
            name = lid or comm                            # 现房显示房源ID（型号）
        g = groups.get(key)
        if not g:
            g = groups[key] = {"type": key[0], "name": name, "community": comm,
                               "huxing": huxing, "agg": _blank_video()}
        _add_video(g["agg"], r)

    # 房源级线索：CRM listing_id → 现房按房源ID、期房按社区名并入房源维度
    lead_ready, lead_offplan = aggregate_leads_by_listing(leads, map_offplan)

    def _lead3(d):
        return {k: d[k] for k in ("newLeads", "qualified", "meetings")} if d else _blank_lead()

    listings_out = []
    for (typ, _), g in groups.items():
        a = g["agg"]
        # 现房行按房源ID、期房行按社区名 并入线索
        ld = lead_ready.pop(g["name"], None) if g["type"] == "现房" else lead_offplan.pop(g["name"], None)
        listings_out.append({
            "id": g["name"],
            "type": g["type"],                 # 期房 / 现房
            "name": g["name"],                 # 期房=社区名；现房=房源ID
            "community": g["community"],       # 现房的所属小区（副信息）
            "huxing": g["huxing"],
            **{k: round(a[k]) for k in ("videos", "totalVV", "maxVV", "likes", "comments", "shares")},
            "dms": 0, "replies": 0, "whatsapp": 0,   # 私信为账号级，房源维度置0
            **_lead3(ld),
        })
    # 仅有线索、没有视频的房源/社区，补成独立行
    lead_only = 0
    for lid, ld in lead_ready.items():
        lead_only += 1
        listings_out.append({
            "id": lid, "type": "现房", "name": lid,
            "community": ld["community"], "huxing": ld["huxing"],
            "videos": 0, "totalVV": 0, "maxVV": 0, "likes": 0, "comments": 0, "shares": 0,
            "dms": 0, "replies": 0, "whatsapp": 0,
            **_lead3(ld),
        })
    for comm, ld in lead_offplan.items():
        lead_only += 1
        listings_out.append({
            "id": comm, "type": "期房", "name": comm,
            "community": comm, "huxing": "",
            "videos": 0, "totalVV": 0, "maxVV": 0, "likes": 0, "comments": 0, "shares": 0,
            "dms": 0, "replies": 0, "whatsapp": 0,
            **_lead3(ld),
        })
    listings_out.sort(key=lambda x: (x["totalVV"], x["newLeads"]), reverse=True)
    print(f"[房源] 视频归因 {matched} 条 → {len(groups)} 房源/社区"
          f"(无码 {no_code}, 未查到 {unresolved}, 非期房/现房 {other_cat}) · 纯线索补 {lead_only} 行")

    # 6) 按天明细（前端按所选日期区间真实聚合；解析走缓存，不重复请求）
    videos_out = []
    for r in videos:
        title = r.get("视频标题") or ""
        row = {"d": (r.get("视频发布时间") or "")[:10],
               "h": r.get("账号ID") or "",
               "vv": round(_num(r.get(VID_FIELDS["totalVV"]))),
               "lk": round(_num(r.get(VID_FIELDS["likes"]))),
               "cm": round(_num(r.get(VID_FIELDS["comments"]))),
               "sh": round(_num(r.get(VID_FIELDS["shares"])))}
        d = resolve_video_listing(title) if PROP_CODE_RE.search(title) else None
        if d:
            cat = (d.get("类别") or "").strip()
            # 口径：仅类别=期房/现房 参与房源维度；租房/售房等不打房源标签
            if cat in ("期房", "现房"):
                comm = (d.get("小区名称") or "").strip() or "(未知小区)"
                lid = (d.get("房源ID") or "").strip()
                if cat == "期房":
                    row["lt"], row["ln"] = "期房", map_offplan(comm)
                else:
                    row["lt"], row["ln"] = "现房", lid or comm
                row["lc"] = comm
                row["lx"] = (d.get("户型") or "").strip()
        videos_out.append(row)
    lead_rows, lead_listing_meta = leads_detail(leads, targets, map_offplan)
    print(f"[明细] 视频 {len(videos_out)} 条 · 线索 {len(lead_rows)} 条(可归因)")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": WINDOW_DAYS,
                   "snapshot": latest_snapshot},   # VidFlow T+1 实际最新快照日
        "accounts": accounts_out,
        "listings": listings_out,
        "videos": videos_out,          # 按天明细：前端据此按日期区间聚合
        "leads": lead_rows,
        "listing_meta": lead_listing_meta,
        "coverage": {"videos": len(videos), "matched": matched,
                     "no_code": no_code, "unresolved": unresolved},
        "notes": {
            "leads": ("线索来自 CRM，按来源账号聚合：新增=全部线索，合格=进入Qualified及以后，约面=进入Viewing及以后"
                      if CRM_FULL_KEY else "线索环节占位0（未设置 CRM_FULL_KEY）"),
            "listing_leads": "现房线索按 CRM listing_id(房源ID)归因；期房线索、私信仍为账号级，房源维度置0",
            "listing_attribution": "视频标题房源码 → GET /listings/<码> 关联房源；期房按社区名、现房按小区名+户型聚合",
        },
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_JS.write_text("window.DASHBOARD_DATA = " +
                      json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"\n✅ 写出 {OUT_JS.name} / {OUT_JSON.name}")
    print(f"   账号 {len(accounts_out)} 条，期房社区 {len(listings_out)} 条")


if __name__ == "__main__":
    asyncio.run(main())
