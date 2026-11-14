# -*- coding: utf-8 -*-
"""
state_check.py — 状态库机械校验脚本（v1.3.0 · 只报告，不阻断）
v1.3.0：REQUIRED_FILES 补 hooks.md / regression-tests.md / project/state-schema.md
（14→17）；F01 文案改「20 项中必查 17 项」，不再让报错数字大于实际检查数。
v1.2.0（v1.6.2 配套）：闭集收编「待埋」（hooks 增 已作废）；新增 T06 埋设计划超期
（待埋 且 计划埋设章 < 当前章 → WARN：补埋/改计划/作废 三选一）；T02/T03 按新活跃规则
（待埋、已作废不计活跃）更新措辞与判定。
用法:
    python scripts/state_check.py --dir <任务目录> [--chapter N] [--schema <path>] [--json] [--strict]
    --dir      任务目录（含 characters.md / hooks.md / foreshadowing.md / timeline.md 等）
    --chapter  当前章号；缺省时取同目录 chapter_<n>.md 的最大编号
    --schema   project/state-schema.md 路径；缺省 <dir>/project/state-schema.md
    --json     只输出 JSON
    --strict   有 ERROR 时返回码 1（默认恒为 0：只报告，不阻断写作）
输出:
    人读报告写入 <dir>/state_check_report.md；摘要打印到 stdout
覆盖范围（全量）:
  F 组 格式：文件存在性 / 表头缺列 / 编号重复与跳号 / 状态闭合枚举 / 必填空格
  T 组 时间：伏笔超期未回收 / 钩子超期未兑现 / 活跃标记与规则不符 / 出场调度超期 / 时间线章号断档
  X 组 交叉：RT-xxx 与 Hxx 引用是否存在 / 快照枚举越界 / 快照角色未登记 / 已回收但无实际回收章
级别约定:
  ERROR  数据结构不可用或违反闭合枚举（必然要修）
  WARN   与规范判定规则冲突（应修，但可能是作者有意为之）
  INFO   启发式提示，需人工确认（别名/化名/代称会造成误报）
依据:
  references/progress-tracking.md 第 8 节（任务目录 20 文件权威清单；本脚本必查其中 17 项）
  references/state-templates.md（hooks.md / regression-tests.md 表头）
  references/world-consistency.md（伏笔表七列与六状态）
  SKILL.md「核心机制」（伏笔表状态闭集、活跃钩子判定）
"""
import argparse
import json
import os
import re
import sys
import datetime

ZW = "\u200b\u200c\u200d\u2060\ufeff\u00a0"

# —— 权威文件清单（progress-tracking.md 第 8 节 20 项中的 17 项必查；快照按章通配、style-fingerprint / relationship-ledger 无机械校验，不列入）——
REQUIRED_FILES = [
    "characters.md", "settings.md", "foreshadowing.md", "timeline.md",
    "rolling_summary.md", "expansion_evolution_log.md", "theme.md",
    "style_anchor.md", "subplots.md", "satisfaction_points.md", "dialogue_log.md",
    "hooks.md", "regression-tests.md",
    "constraints/author-decisions.md", "state/retcon-log.md",
    "state/emotion-curve.md", "project/state-schema.md",
]

# 文档未统一规定状态库文件路径，真实项目存在多种布局
# （根目录 / state/ / constraints/ / project/…），按候选顺序解析
DIR_CANDIDATES = ("", "state/", "constraints/", "project/",
                  "project/constraints/", "project/state/")


def locate(root, rel):
    """多布局文件定位：返回相对路径，找不到返回 None"""
    base = os.path.basename(rel)
    cands = [rel] + [d + base for d in DIR_CANDIDATES]
    seen = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    for c in seen:
        if os.path.exists(os.path.join(root, c)):
            return c
    return None

# —— 表头契约：文件 → [(逻辑列名, 关键字集合)] ——
SCHEMAS = {
    "hooks.md": [
        ("id", ("编号",)),
        ("content", ("钩子内容", "内容", "钩子")),
        ("planted", ("埋设章", "埋设")),
        ("due", ("预期回收章", "预期", "回收章")),
        ("status", ("状态",)),
        ("active", ("活跃",)),
        ("touched", ("最近触碰", "触碰章")),
    ],
    "foreshadowing.md": [
        ("content", ("伏笔", "内容")),
        ("planted", ("埋设章", "埋设")),
        ("technique", ("技法",)),
        ("status", ("状态",)),
        ("due", ("计划回收", "计划")),
        ("actual", ("实际回收", "实际")),
    ],
    "regression-tests.md": [
        ("id", ("编号",)),
        ("assertion", ("断言", "项")),
        ("method", ("判定",)),
        ("source", ("来源章", "来源")),
        # 不把「生效」当状态列别名：老项目常把 状态/来源 合并成一列，
        # 结构问题应由 F02 报一条，而不是把每行取值炸成 F05
        ("status", ("状态",)),
    ],
    "state/cast-schedule.md": [
        ("who", ("角色", "线")),
        ("last", ("上次露面",)),
        ("gap", ("间隔上限", "间隔")),
        ("next", ("下次必须", "必须露面")),
    ],
}

# —— 闭合枚举（违反即 ERROR）——
STATUS_ENUM = {
    # 待埋 = 已登记计划、正文未出现（v1.6.2 起收编为第 0 态）
    "hooks.md": ("待埋", "未兑现", "部分兑现", "已兑现", "已作废"),
    "foreshadowing.md": ("待埋", "待回收", "已回收", "标记修正", "已核销", "已作废", "悬置"),
    "regression-tests.md": ("生效", "停用"),
}

CHAP_RE = re.compile(r"(?:第\s*)?(\d+)\s*章")
NUM_RE = re.compile(r"(\d+)")
ID_H_RE = re.compile(r"H\d+", re.I)
ID_RT_RE = re.compile(r"RT-\d+", re.I)

# 占位符：这些值等同「空」。不折算会把「—」当成已填，导致超期/一致性判定整体失效
EMPTY_TOKENS = {"", "—", "-", "——", "－", "–", "/", "无", "空", "暂无", "待补", "N/A", "n/a"}


def blank(v):
    return clean(v).strip("。；; ") in EMPTY_TOKENS

findings = []


def add(code, level, where, msg, evidence=""):
    findings.append({
        "code": code, "level": level, "where": where, "msg": msg,
        "evidence": evidence[:160],
    })


def clean(s):
    # 去零宽字符 + 去行内 markdown 强调符号（**加粗** 不得影响枚举/列名判定）
    return str(s).translate({ord(c): None for c in ZW}).replace("*", "").strip()


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def chnum(cell):
    """从单元格取章号：支持「第3章」「3」「第 3 章」；取不到返回 None"""
    if not cell:
        return None
    m = CHAP_RE.search(cell) or NUM_RE.search(cell)
    return int(m.group(1)) if m else None


def tables(text):
    """解析 markdown 表格，返回 [{'header': [...], 'rows': [[...]]}]"""
    blocks, cur = [], None
    for raw in text.split("\n"):
        t = clean(raw)
        if t.startswith("|") and t.count("|") >= 2:
            cells = [clean(c) for c in t.strip("|").split("|")]
            if cur is None:
                cur = {"header": cells, "rows": []}
            elif all(c and set(c) <= set("-: ") for c in cells):
                continue
            else:
                cur["rows"].append(cells)
        else:
            if cur:
                blocks.append(cur)
                cur = None
    if cur:
        blocks.append(cur)
    return blocks


def map_cols(header, spec):
    """按关键字把逻辑列映射到物理下标；找不到返回 -1"""
    idx = {}
    for logical, keys in spec:
        idx[logical] = -1
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                idx[logical] = i
                break
    return idx


def cell(row, i):
    return row[i] if 0 <= i < len(row) else ""


def check_schema(fname, blocks):
    """表头缺列 + 返回最后一张匹配表的 (cols, rows)"""
    spec = SCHEMAS.get(fname)
    if not spec or not blocks:
        return None
    best = None
    for b in blocks:
        cols = map_cols(b["header"], spec)
        hit = sum(1 for v in cols.values() if v >= 0)
        if best is None or hit > best[0]:
            best = (hit, cols, b)
    if best is None:
        return None
    hit, cols, b = best
    missing = [lg for lg, i in cols.items() if i < 0]
    if missing:
        add("F02", "ERROR", fname,
            f"表头缺列：{'、'.join(missing)}",
            "实际表头：" + " | ".join(b["header"]))
    return cols, b["rows"]


def check_ids(fname, rows, cols, idlogical="id", prefix=""):
    seen, nums = {}, []
    for n, r in enumerate(rows, 1):
        raw = cell(r, cols.get(idlogical, -1))
        if not raw:
            continue
        if raw in seen:
            add("F03", "ERROR", f"{fname} 第{n}行",
                f"{prefix}编号重复：{raw}（首次出现于第{seen[raw]}行）", raw)
        else:
            seen[raw] = n
        if prefix:
            m = re.search(re.escape(prefix) + r"(\d+)", raw, re.I)
            if m:
                nums.append(int(m.group(1)))
    if len(nums) >= 3:
        full = set(range(min(nums), max(nums) + 1))
        gap = sorted(full - set(nums))
        if gap:
            add("F04", "INFO", fname,
                f"{prefix} 编号跳号：{', '.join(prefix + str(g).zfill(2) for g in gap[:8])}",
                f"现有 {min(nums)}–{max(nums)}，缺 {len(gap)} 个")


def check_status(fname, rows, cols):
    allowed = STATUS_ENUM.get(fname)
    if not allowed:
        return
    for n, r in enumerate(rows, 1):
        s = cell(r, cols.get("status", -1))
        if not s:
            continue
        if not any(s.startswith(a) for a in allowed):
            add("F05", "ERROR", f"{fname} 第{n}行",
                f"状态取值非法：{s!r}（闭集：{' / '.join(allowed)}）", s)


def check_required(fname, rows, cols, required):
    for n, r in enumerate(rows, 1):
        for lg, label in required:
            i = cols.get(lg, -1)
            if i >= 0 and blank(cell(r, i)):
                add("F06", "WARN", f"{fname} 第{n}行", f"必填项为空：{label}", "")


def check_foreshadow_due(rows, cols, cur):
    for n, r in enumerate(rows, 1):
        st = cell(r, cols.get("status", -1))
        due = chnum(cell(r, cols.get("due", -1)))
        planted = chnum(cell(r, cols.get("planted", -1)))
        actual = cell(r, cols.get("actual", -1))
        content = cell(r, cols.get("content", -1))
        done = not blank(actual)
        if st.startswith(("待回收", "标记修正")) and due and cur and due < cur and not done:
            add("T01", "WARN", f"foreshadowing.md 第{n}行",
                f"伏笔超期未回收：计划第{due}章回收，当前第{cur}章，已超 {cur - due} 章",
                content)
        # T06 埋设计划超期：待埋 且 计划埋设章 < 当前章 → 补埋 / 改计划 / 作废，三选一
        if st.startswith("待埋") and planted and cur and planted < cur:
            add("T06", "WARN", f"foreshadowing.md 第{n}行",
                f"埋设计划超期：计划第{planted}章埋设，当前第{cur}章正文未出现——补埋 / 改计划埋设章 / 作废计划，三选一",
                content)
        if st.startswith(("已回收", "已核销")) and not done:
            add("X04", "ERROR", f"foreshadowing.md 第{n}行",
                f"状态为「{st}」但实际回收章为空，数据不一致", content)


def check_hooks_due(rows, cols, cur):
    for n, r in enumerate(rows, 1):
        st = cell(r, cols.get("status", -1))
        due = chnum(cell(r, cols.get("due", -1)))
        planted = chnum(cell(r, cols.get("planted", -1)))
        active = cell(r, cols.get("active", -1))
        content = cell(r, cols.get("content", -1))
        if not cur:
            continue
        open_hook = st.startswith(("未兑现", "部分兑现"))
        if open_hook and due and due < cur:
            add("T02", "WARN", f"hooks.md 第{n}行",
                f"钩子超期未兑现：预期第{due}章回收，当前第{cur}章，已超 {cur - due} 章",
                content)
        # T06 埋设计划超期：待埋 且 计划埋设章 < 当前章 → 补埋 / 改计划 / 作废，三选一
        if st.startswith("待埋") and planted and planted < cur:
            add("T06", "WARN", f"hooks.md 第{n}行",
                f"埋设计划超期：计划第{planted}章埋设，当前第{cur}章正文未出现——补埋 / 改计划埋设章 / 作废计划，三选一",
                content)
        # 规范（v1.6.2）：活跃 = 未兑现或部分兑现 且 未超期；待埋、已兑现、已作废或超期 = 不活跃
        should_active = open_hook and due >= cur
        said_active = active.strip().startswith(("活跃", "是", "Y", "y"))
        said_inactive = active.strip().startswith(("不活跃", "否", "N", "n"))
        if said_active and not should_active:
            add("T03", "WARN", f"hooks.md 第{n}行",
                f"活跃标记与规则冲突：标记为「活跃」，但按规范（未兑现/部分兑现 且 当前章≤预期回收章；待埋不计活跃）应为不活跃",
                f"状态={st} 预期={due} 当前={cur}")
        if said_inactive and should_active:
            add("T03", "WARN", f"hooks.md 第{n}行",
                "活跃标记与规则冲突：标记为「不活跃」，但未兑现且未超期，应为活跃",
                f"状态={st} 预期={due} 当前={cur}")


def check_cast(rows, cols, cur):
    for n, r in enumerate(rows, 1):
        who = cell(r, cols.get("who", -1))
        last = chnum(cell(r, cols.get("last", -1)))
        gap = chnum(cell(r, cols.get("gap", -1)))
        nxt = chnum(cell(r, cols.get("next", -1)))
        if not cur:
            continue
        if last and gap and last + gap < cur:
            add("T04", "WARN", f"state/cast-schedule.md 第{n}行",
                f"出场调度超期：上次露面第{last}章，间隔上限{gap}章，当前第{cur}章，应于第{last + gap}章前露面",
                who)
        if nxt and nxt < cur:
            add("T04", "WARN", f"state/cast-schedule.md 第{n}行",
                f"出场调度超期：下次必须露面章为第{nxt}章，当前已到第{cur}章", who)


def check_timeline_breaks(text, cur):
    if not text:
        return
    nums = sorted({int(m.group(1)) for m in CHAP_RE.finditer(text)})
    if len(nums) < 3:
        return
    gap = sorted(set(range(nums[0], nums[-1] + 1)) - set(nums))
    if gap:
        add("T05", "INFO", "timeline.md",
            f"时间线章号不连续，缺 {len(gap)} 章：{', '.join('第%d章' % g for g in gap[:8])}",
            f"覆盖 {nums[0]}–{nums[-1]}")


def check_refs(dirpath, cur):
    """X01：被引用的 RT-xxx / Hxx 是否在对应表登记。

    目标文件缺失时不静默跳过——引用无法核验本身就是要报的问题。
    """
    rt_actual = locate(dirpath, "state/retcon-log.md")
    rt_text = read(os.path.join(dirpath, rt_actual)) if rt_actual else None
    h_actual = locate(dirpath, "hooks.md")
    h_text = read(os.path.join(dirpath, h_actual)) if h_actual else None

    rt_ids = {m.group(0).upper() for m in ID_RT_RE.finditer(rt_text or "")}
    h_ids = {m.group(0).upper() for m in ID_H_RE.finditer(h_text or "")}

    fs_actual = locate(dirpath, "foreshadowing.md")
    fs_text = read(os.path.join(dirpath, fs_actual)) if fs_actual else ""
    for m in ID_RT_RE.finditer(fs_text):
        rid = m.group(0).upper()
        if rt_text is None:
            add("X01", "ERROR", "foreshadowing.md",
                f"引用了修订编号 {rid}，但 state/retcon-log.md 不存在，无法核验",
                "「已作废（被 RT-xxx 替代）」的 RT 必须在 retcon-log 登记")
        elif rid not in rt_ids:
            add("X01", "ERROR", "foreshadowing.md",
                f"引用了未登记的修订编号：{rid}（state/retcon-log.md 中不存在）", "")

    for fname in ("foreshadowing.md", "timeline.md", "subplots.md"):
        actual = locate(dirpath, fname)
        t = read(os.path.join(dirpath, actual)) if actual else None
        if t is None:
            continue
        for m in ID_H_RE.finditer(t):
            hid = m.group(0).upper()
            if h_text is None:
                add("X01", "ERROR", fname,
                    f"引用了钩子编号 {hid}，但 hooks.md 不存在，无法核验", "")
            elif hid not in h_ids:
                add("X01", "ERROR", fname,
                    f"引用了未登记的钩子编号：{hid}（hooks.md 中不存在）", "")


def parse_enums(schema_text):
    """从 project/state-schema.md 取 position_enum / posture_enum / prop_list 取值"""
    out = {}
    if not schema_text:
        return out
    cur = None
    for raw in schema_text.split("\n"):
        line = clean(raw)
        m = re.match(r"^#{1,4}\s*([a-z_]+)", line)
        if m:
            cur = m.group(1)
            if cur in ("position_enum", "posture_enum", "prop_list"):
                out.setdefault(cur, [])
            continue
        if cur in out and line:
            # 表格行（如 prop_list 常写成「| 道具 | 说明 |」）：只取首列取值，
            # 跳过表头与分隔行——说明列自由文本绝不能混进枚举白名单
            if line.startswith("|"):
                cells = [clean(c) for c in line.strip("|").split("|")]
                if cells and all(c and set(c) <= set("-: ") for c in cells):
                    continue
                first = cells[0] if cells else ""
                if first and not set(first) <= set("-: ") \
                        and first not in ("道具", "取值", "位置", "姿态"):
                    out[cur].append(first.strip("`'\"。；;"))
                continue
            t = re.sub(r"^[-*+]\s*", "", line).strip()
            for tok in re.split(r"[、,，/｜|\s]+", t):
                tok = tok.strip("`'\"。；;")
                if tok and not tok.startswith("#"):
                    out[cur].append(tok)
    return {k: [v for v in vals if v] for k, vals in out.items()}


def check_snapshot(dirpath, schema_path):
    """X02：快照的位置/姿态/道具是否落在 state-schema 枚举内"""
    enums = parse_enums(read(schema_path))
    pos = set(enums.get("position_enum", []))
    post = set(enums.get("posture_enum", []))
    props = set(enums.get("prop_list", []))
    if not (pos or post or props):
        add("X02", "INFO", "project/state-schema.md",
            "未解析到 position_enum / posture_enum / prop_list，快照枚举校验已跳过", "")
        return
    # 快照目录同样多布局：state/ 或 project/state/
    snap_files = []
    for d in ("state", os.path.join("project", "state")):
        sdir = os.path.join(dirpath, d)
        if os.path.isdir(sdir):
            snap_files += [(sdir, fn) for fn in sorted(os.listdir(sdir))
                           if re.match(r"(chapter_\d+|unit_\d+)_snapshot\.md$", fn)]
    if not snap_files:
        return
    for sdir, fn in snap_files:
        path = os.path.join(sdir, fn)
        for b in tables(read(path) or ""):
            cols = map_cols(b["header"], [
                ("pos", ("位置",)), ("post", ("姿态",)), ("prop", ("道具",)),
            ])
            for n, r in enumerate(b["rows"], 1):
                for logical, allowed, label, lvl in (
                        ("pos", pos, "位置", "ERROR"),
                        ("post", post, "姿态", "ERROR"),
                        # prop_list 语义是「反复出现的关键道具」开放登记表，
                        # 非闭合枚举：未登记只提示，一次性道具属正常
                        ("prop", props, "道具", "WARN")):
                    i = cols.get(logical, -1)
                    v = cell(r, i) if i >= 0 else ""
                    if not v or not allowed:
                        continue
                    if v not in allowed and not any(v.startswith(a) for a in allowed):
                        add("X02", lvl, f"state/{fn} 第{n}行",
                            f"{label}取值{'不在 state-schema 枚举内' if lvl == 'ERROR' else '未在 prop_list 登记（若为一次性道具可忽略；反复出现则应登记）'}：{v!r}",
                            f"白名单前 6 项：{'、'.join(list(allowed)[:6])}")


def registered_names(dirpath):
    """从 characters.md 的表格首列与「**名字**」形式抽取已登记角色名"""
    c_actual = locate(dirpath, "characters.md")
    text = read(os.path.join(dirpath, c_actual)) if c_actual else ""
    names = set()
    for b in tables(text):
        if len(b["rows"]) >= 2:
            for r in b["rows"]:
                v = cell(r, 0)
                if 1 < len(v) <= 6 and not re.search(r"\d", v):
                    names.add(v)
    for m in re.finditer(r"\*\*([^\*]{1,6})\*\*", text):
        names.add(clean(m.group(1)))
    return names


def check_snapshot_cast(dirpath):
    """X03：快照「角色」列出现但 characters.md 未登记（启发式，INFO）"""
    names = registered_names(dirpath)
    if not names:
        return
    unknown = {}
    for d in ("state", os.path.join("project", "state")):
        sdir = os.path.join(dirpath, d)
        if not os.path.isdir(sdir):
            continue
        for fn in sorted(os.listdir(sdir)):
            if not re.match(r"(chapter_\d+|unit_\d+)_snapshot\.md$", fn):
                continue
            for b in tables(read(os.path.join(sdir, fn)) or ""):
                cols = map_cols(b["header"], [("who", ("角色", "人物"))])
                i = cols.get("who", -1)
                if i < 0:
                    continue
                for r in b["rows"]:
                    for tok in re.split(r"[、,，/＋+及与\s]+", cell(r, i)):
                        tok = clean(tok)
                        if 1 < len(tok) <= 5 and tok not in names:
                            unknown.setdefault(tok, 0)
                            unknown[tok] += 1
    for name, cnt in sorted(unknown.items(), key=lambda x: -x[1])[:10]:
        add("X03", "INFO", "state/*_snapshot.md",
            f"快照出现未在 characters.md 登记的角色名：{name}（{cnt} 次）",
            "可能是别名/化名/代称，需人工确认")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--chapter", type=int, default=0)
    ap.add_argument("--schema", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(json.dumps({"status": "FAIL", "reason": f"目录不存在: {root}"}, ensure_ascii=False))
        return 1

    # 当前章号：显式 > chapter_<n>.md 最大值
    cur = args.chapter
    if not cur:
        nums = [int(m.group(1)) for m in
                (re.match(r"chapter_(\d+)\.md$", f) for f in os.listdir(root)) if m]
        cur = max(nums) if nums else 0

    schema_path = args.schema
    if not schema_path:
        for c in ("project/state-schema.md", "state-schema.md",
                  "project/state-schema.draft.md", "state-schema.draft.md"):
            p = os.path.join(root, c)
            if os.path.exists(p):
                schema_path = p
                break
        schema_path = schema_path or os.path.join(root, "project", "state-schema.md")

    # F01 文件存在性（聚合成一条，避免 17 条 INFO 淹没真问题；多布局解析）
    missing = [rel for rel in REQUIRED_FILES if not locate(root, rel)]
    if missing:
        add("F01", "INFO", "任务目录",
            f"权威清单 20 项中必查 17 项（3 项不适用：快照按章通配、style-fingerprint / relationship-ledger 无机械校验），缺 {len(missing)} 个（新建项目未到该阶段可忽略）",
            "、".join(missing[:10]) + ("…" if len(missing) > 10 else ""))

    parsed = {}
    for fname in ("hooks.md", "foreshadowing.md", "regression-tests.md",
                  "state/cast-schedule.md"):
        actual = locate(root, fname)
        if actual is None:
            continue
        text = read(os.path.join(root, actual))
        if text is None:
            continue
        res = check_schema(fname, tables(text))
        if res:
            parsed[fname] = res

    # F 组：编号 / 状态 / 必填
    if "hooks.md" in parsed:
        cols, rows = parsed["hooks.md"]
        check_ids("hooks.md", rows, cols, "id", "H")
        check_status("hooks.md", rows, cols)
        check_required("hooks.md", rows, cols,
                       [("content", "钩子内容"), ("planted", "埋设章"), ("due", "预期回收章")])
        check_hooks_due(rows, cols, cur)

    if "foreshadowing.md" in parsed:
        cols, rows = parsed["foreshadowing.md"]
        check_status("foreshadowing.md", rows, cols)
        check_required("foreshadowing.md", rows, cols,
                       [("content", "伏笔内容"), ("planted", "埋设章"), ("due", "计划回收章")])
        check_foreshadow_due(rows, cols, cur)

    if "regression-tests.md" in parsed:
        cols, rows = parsed["regression-tests.md"]
        check_ids("regression-tests.md", rows, cols, "id", "RT-")
        check_status("regression-tests.md", rows, cols)

    if "state/cast-schedule.md" in parsed:
        cols, rows = parsed["state/cast-schedule.md"]
        check_cast(rows, cols, cur)

    tl_actual = locate(root, "timeline.md")
    check_timeline_breaks(
        read(os.path.join(root, tl_actual)) if tl_actual else "", cur)
    check_refs(root, cur)
    check_snapshot(root, schema_path)
    check_snapshot_cast(root)

    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (order[f["level"]], f["code"], f["where"]))
    counts = {lv: sum(1 for f in findings if f["level"] == lv) for lv in ("ERROR", "WARN", "INFO")}

    if args.json:
        print(json.dumps({"dir": root, "chapter": cur, "counts": counts,
                          "findings": findings}, ensure_ascii=False, indent=2))
        return 1 if (args.strict and counts["ERROR"]) else 0

    lines = [
        "# 状态库检查报告", "",
        f"- 任务目录：`{root}`",
        f"- 当前章：第 {cur} 章" if cur else "- 当前章：未指定（跳过时间维度判定）",
        f"- 检查时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结论：**ERROR {counts['ERROR']} / WARN {counts['WARN']} / INFO {counts['INFO']}**",
        "",
        "> 本报告只记录，不阻断写作。ERROR 属数据结构问题（必然要修）；",
        "> WARN 属与规范判定规则冲突（可能是有意为之）；INFO 为启发式提示（需人工确认）。",
        "",
    ]
    if not findings:
        lines.append("未发现问题。")
    for lv, title in (("ERROR", "ERROR · 结构错误"), ("WARN", "WARN · 规则冲突"), ("INFO", "INFO · 提示")):
        group = [f for f in findings if f["level"] == lv]
        if not group:
            continue
        lines += [f"## {title}（{len(group)}）", "",
                  "| 代码 | 位置 | 问题 | 证据 |", "|---|---|---|---|"]
        for f in group:
            ev = f["evidence"].replace("|", "/")
            lines.append(f"| `{f['code']}` | {f['where']} | {f['msg']} | {ev} |")
        lines.append("")

    report_path = os.path.join(root, "state_check_report.md")
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[warn] 报告写入失败: {e}", file=sys.stderr)

    print("\n".join(lines[:9]))
    print(f"完整报告：{report_path}")
    return 1 if (args.strict and counts["ERROR"]) else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
