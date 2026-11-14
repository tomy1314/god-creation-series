# -*- coding: utf-8 -*-
"""
guard.py — 长篇扩写强制校验脚本（v1.4.5 · 双栖引擎 · 检测器加固 ＋ 本书口径覆盖层）
用法:
    python scripts/guard.py '{"current_chapter":1,"target_words":200000,"total_words":0,"chapter_file":"chapter_1.md"}'
    双栖引擎：追加 "mode":"literary"（或 "work_mode":"literary"）走传统文学红线
    自动续写：追加 "mode":"auto"
    本书口径：追加 "thresholds_override":{"dialogue_ratio":[0.20,0.30],"max_consecutive_dialogue":2}
    不传 total_words（或传 0）时，自动扫描 chapter_file 同目录的 chapter_<n>.md 累加汉字数
返回 JSON: {status, reason, details, mode}
规则（web 基线；literary 见参数约定）:
  - FAIL: 汉字数超出 [2000,5000]；台词占比超出 [8%,55%]；分析性旁白 >12%；
          连续无提示语对话 >6 行；章节文件缺失
  - PAUSE: 进度 <95% 且章末命中收束性语句（强信号全文扫／弱信号仅章末 2 段）；
           自动续写第 5n 章强制暂停；台词体无法识别（需人工确认，不按 0% 误判 FAIL）
  - PASS: 全部通过
  - 本脚本只覆盖机械可量化项；v1.4.5 新增的技法标注/7.6-8 叙事技法/
    冰山/客观关联物/留白 均为模型判定项，不在此脚本范围。

v1.4.4 变更（检测器加固，行为向后兼容）:
  1. 台词识别扩至三种引号体：双引号“”、直角引号「」、直角引号『』。
     旧版仅识别双引号，用「」写作的稿件台词占比恒为 0%，会被 web 下限误杀。
     单引号‘’不计入台词（多用于强调/术语，计入会虚高），仅在 quote_styles 中报告。
  2. 台词体识别不出时不再静默按 0% 判 FAIL，改判 PAUSE 并要求人工确认，
     避免"检测器看不见"被当成"作者写错了"。
  3. 收尾信号由 6 个词扩为两级：强信号（全书完/大结局类）全文扫描；
     弱信号（多年后/再也没/归于平静/此后 N 年等）只在章末 2 段内判，
     避免章中合法时间跳跃被误拦。命中项写入 details.closing_hits。
  4. 分析性旁白先剥离引号内台词再判（过去对话里的"换句话说"会被计入旁白）；
     新增标记词，并把 显然/事实上/总之 归入软标记，只报告不参与 FAIL。
  5. total_words 缺省时自动扫描同目录 chapter_<n>.md 累加（仅计 ≤ 当前章），
     不再把长程算数单方面退回调用方；传入值仍优先。

v1.4.5 变更（本书口径覆盖层，补齐 quality-audit 7.4 第 3 层）:
  6. 新增 thresholds_override 参数，把「本书口径（覆盖层，优先级最高）」从纸面变成可执行。
     可覆盖 4 个量化阈值：han / dialogue_ratio / analytical_ratio_max / max_consecutive_dialogue；
     优先级：覆盖层 > 引擎基线（web/literary/cinematic）。
     覆盖层放宽超出全局红线时，只写 details.red_line_warnings 作兜底警报，不改判——
     与 quality-audit.md 第 99 行「三层冲突时按本书口径判，红线只作兜底警报」一致。
     closing_check 不接受覆盖：「禁止提前收尾」是最高优先级硬约束，不允许经参数关闭。
     非法键/非法值一律忽略并写入 warnings，不 silent pass。
     生效结果写入 details.thresholds.source（engine / overlay）与 override_applied。
# 兼容技能版本：1.6.1（脚本版本线独立记为 v1.4.5，不与技能版本同步）
参数约定（务必与 SKILL.md 一致）：
  - total_words：含本章的累计汉字数。传入则以此为准（调用方负责累加）；
    缺省/0 时脚本自动扫描累加，details.total_words_source 标明来源
  - mode：双栖引擎主开关。web（默认，商业网文红线）/ literary（传统文学：
          字数 2000-8000、台词 5%-65%、旁白 25%、取消提前收尾拦截）/ cinematic（暂沿用 web 基线）；
          同时兼容旧的 "auto"/"manual" 续写开关写法（此时 work_mode 回落 web）
  - work_mode：显式指定文体模式，优先级高于 mode（0c-0 写入 state-schema 的字段名即此名）
  - thresholds_override：本书口径覆盖层（可选），形如
          {"dialogue_ratio":[0.20,0.30],"max_consecutive_dialogue":2}。
          优先级高于 work_mode 基线；可覆盖 han／dialogue_ratio／analytical_ratio_max／
          max_consecutive_dialogue 四项；closing_check 不接受覆盖；超全局红线只作兜底告警。
          生效结果见 details.thresholds.source（engine／overlay）与 override_applied。
  - 两个键都缺省时，按 web（商业网文）判定，与历史行为一致
  - 字数口径：纯汉字，不含标点、字母、数字
"""
import glob
import json
import os
import re
import sys

MIN_HAN = 2000
MAX_HAN = 5000
MIN_DIALOGUE_RATIO = 0.08
MAX_DIALOGUE_RATIO = 0.55
MAX_ANALYTICAL_RATIO = 0.12
MAX_CONSECUTIVE_DIALOGUE = 6

# 覆盖层（本书口径）可覆盖的键。closing_check 不在其列：
# 「禁止提前收尾」是最高优先级硬约束，不允许通过参数关闭
OVERRIDE_SPEC = {
    "han": "range",
    "dialogue_ratio": "range",
    "analytical_ratio_max": "ratio",
    "max_consecutive_dialogue": "int",
}
# 全局红线：覆盖层放宽超出这些值时只作兜底告警，不改判（quality-audit 7.4 第 3 层）
RED_LINE = {
    "dialogue_ratio": (MIN_DIALOGUE_RATIO, MAX_DIALOGUE_RATIO),
    "analytical_ratio_max": MAX_ANALYTICAL_RATIO,
    "max_consecutive_dialogue": MAX_CONSECUTIVE_DIALOGUE,
}

# 收尾强信号：全文任何位置出现都判「已收尾」，无误报空间
CLOSING_STRONG = [
    "全书完", "全文完", "全剧终", "大结局",
    "故事到这里就结束了", "本书到此为止", "本书完",
]

# 收尾弱信号：只在章末窗口内判，避免章中合法时间跳跃被误拦
CLOSING_WEAK = [
    "多年以后", "多年后", "多年之後", "多年之后",
    "再也没有见过", "再也没见过", "再没回过", "再也没有回来",
    "归于平静", "尘埃落定", "一切都结束了", "就此结束",
    "故事结束", "各自安好", "往后的日子",
]
# 「此后N年」类时间收束
CLOSING_WEAK_RE = re.compile(r"此后\s*(三|五|十|几|数|\d+)\s*年")

# 章末窗口：最后 2 个非空段
CLOSING_WINDOW_PARAS = 2

HAN_RE = re.compile(r"[一-鿿]")
# 台词引号体（单引号''不计入，见 docstring 第 1 条）
QUOTE_PATTERNS = [
    ("double", re.compile(r"“([^”]*)”")),
    ("corner", re.compile(r"「([^」]*)」")),
    ("corner2", re.compile(r"『([^』]*)』")),
    ("single", re.compile(r"‘([^’]*)’")),
]
# 分析性旁白·强标记：命中即判为说明性旁白（参与 12% 红线）
ANALYTICAL_RE = re.compile(
    r"([她他我]明白|[她他我]想起|[她他我]意识到|[她他我]渐渐|"
    r"[她他我]终于明白|这意味着|换句话说|说到底|也就是说|"
    r"换言之|由此可见|这说明|这一切说明|这不难看出|不难看出|"
    r"某种意义上|归根结底|总而言之|原因很简单|说白了)"
)
# 分析性旁白·软标记：只报告，不计入 FAIL（避免"显然/事实上"这类高频词虚高）
ANALYTICAL_SOFT_RE = re.compile(r"(显然|事实上|总之|不难发现|从这里开始)")
# 提示语：用于判断"未识别引号体"
# v1.4.4-r2：原正则只认「说：」冒号式，漏掉中文小说更常见的「他说。」「她道，」
# 故改为「主语＋言说动词＋标点」结构，避免把「他说不清」这类叙述误计入
SPEECH_TAG_RE = re.compile(
    r"(?:[他她我](?:说|道|问|答|喊|叫|吼|开口|回答)|(?:说|道|问|答|喊|叫)[:：])(?=[，。：、；]|$)"
)

# 判断「纯对话段」时要去掉的非汉字字符（用字符集判断，避免正则字符类里引号嵌套出错）
STRIP_PUNCT = set(" \t\r\n，。！？、；：\"'“”‘’（）【】《》…—-～·")


def count_han(s):
    return len(HAN_RE.findall(s))


def dialogue_spans(text):
    """返回 (台词汉字数, 命中的引号体集合, 合并后的区间列表)"""
    spans = []
    styles = []
    for name, pat in QUOTE_PATTERNS:
        found = list(pat.finditer(text))
        if found and name != "single":
            styles.append(name)
        for m in found:
            spans.append((m.start(1), m.end(1)))
    if not spans:
        return 0, styles, []
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    han = sum(count_han(text[s:e]) for s, e in merged)
    return han, styles, merged


def strip_spans(text, spans):
    """把台词区间替换为等长空白，供叙述层分析"""
    if not spans:
        return text
    out, prev = [], 0
    for s, e in spans:
        out.append(text[prev:s])
        out.append("　" * (e - s))
        prev = e
    out.append(text[prev:])
    return "".join(out)


def is_pure_dialogue(p):
    """整段只有引号内容、无叙述文字，视为纯对话段"""
    p = p.strip()
    if not p:
        return False
    rest = p
    hit = False
    for name, pat in QUOTE_PATTERNS:
        if name == "single":
            continue
        new = pat.sub('', rest)
        if new != rest:
            hit = True
        rest = new
    if not hit:
        return False
    rest = "".join(ch for ch in rest if ch not in STRIP_PUNCT)
    return len(rest) == 0


def split_paras(text):
    return [p for p in text.split('\n') if p.strip()]


def analyze(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    han = count_han(text)
    dialogue_han, styles, spans = dialogue_spans(text)
    narration = strip_spans(text, spans)

    sentences = re.split(r'[。！？\n]', narration)
    analytical_han = sum(count_han(s) for s in sentences if ANALYTICAL_RE.search(s))
    soft_han = sum(count_han(s) for s in sentences if ANALYTICAL_SOFT_RE.search(s))

    paras = split_paras(text)
    max_consec, cur = 0, 0
    for p in paras:
        if is_pure_dialogue(p):
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    speech_tag = len(SPEECH_TAG_RE.findall(narration))
    dash_paras = sum(1 for p in paras if p.strip().startswith("——"))

    return {
        "chapter_han": han,
        "dialogue_han": dialogue_han,
        "dialogue_ratio": round(dialogue_han / han, 4) if han else 0.0,
        "analytical_han": analytical_han,
        "analytical_ratio": round(analytical_han / han, 4) if han else 0.0,
        "analytical_soft_ratio": round(soft_han / han, 4) if han else 0.0,
        "max_consecutive_dialogue": max_consec,
        "quote_styles": styles,
        "speech_tags": speech_tag,
        "dash_paragraphs": dash_paras,
    }


def scan_closing(text):
    """返回 (强信号命中, 弱信号命中)"""
    strong = [p for p in CLOSING_STRONG if p in text]
    paras = split_paras(text)
    window = "\n".join(paras[-CLOSING_WINDOW_PARAS:]) if paras else ""
    weak = [p for p in CLOSING_WEAK if p in window]
    weak += ["此后%s年" % m.group(1) for m in CLOSING_WEAK_RE.finditer(window)]
    return strong, weak


def auto_total_words(chapter_file, current_chapter):
    """扫描同目录 chapter_<n>.md 累加汉字数（仅计 ≤ 当前章）"""
    folder = os.path.dirname(os.path.abspath(chapter_file))
    total, used = 0, []
    for f in glob.glob(os.path.join(folder, "chapter_*.md")):
        m = re.search(r"chapter_(\d+)\.md$", os.path.basename(f))
        if not m:
            continue
        idx = int(m.group(1))
        if current_chapter and idx > current_chapter:
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                total += count_han(fh.read())
            used.append(idx)
        except Exception:
            pass
    return total, sorted(used)


def _as_range(v):
    """校验 [min, max] 形态，非法返回 None"""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        a, b = v
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)) and a <= b:
            return [a, b]
    return None


def apply_overrides(ov, base, warnings):
    """覆盖层（本书口径）优先于引擎基线。

    返回 (生效阈值 dict, 实际生效的键, 兜底告警列表)。
    依据 quality-audit.md 第 99 行「本书口径（覆盖层，优先级最高）……三层冲突时按本书口径判，
    红线只作兜底警报」：覆盖层即使放宽超红线也只告警，不改判。
    """
    applied, redline = {}, []
    if ov is None:
        return base, applied, redline
    if isinstance(ov, str):
        try:
            ov = json.loads(ov)
        except Exception as e:
            warnings.append(f"thresholds_override 字符串解析失败，已忽略: {e}")
            return base, applied, redline
    if not isinstance(ov, dict):
        warnings.append("thresholds_override 须为对象，已忽略")
        return base, applied, redline

    out = dict(base)
    for k, v in ov.items():
        kind = OVERRIDE_SPEC.get(k)
        if kind is None:
            if k == "closing_check":
                warnings.append("closing_check 属最高优先级硬约束，不接受覆盖，已忽略")
            else:
                warnings.append(f"thresholds_override 未知键 {k!r}，已忽略")
            continue
        if kind == "range":
            r = _as_range(v)
            if r is None:
                warnings.append(f"{k} 须为 [min, max] 且 min ≤ max，已忽略")
                continue
            out[k] = r
        elif kind == "ratio":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
                out[k] = float(v)
            else:
                warnings.append(f"{k} 须为 0–1 之间的小数，已忽略")
                continue
        elif kind == "int":
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[k] = v
            else:
                warnings.append(f"{k} 须为非负整数，已忽略")
                continue
        applied[k] = out[k]

    # 兜底警报：覆盖层放宽超出全局红线 → 只提示，不阻止（红线仅作警报）
    if "dialogue_ratio" in applied:
        lo, hi = applied["dialogue_ratio"]
        rlo, rhi = RED_LINE["dialogue_ratio"]
        if lo < rlo or hi > rhi:
            redline.append(
                "本书台词口径 %.0f%%–%.0f%% 超出全局红线 %.0f%%–%.0f%%（红线兜底警报）"
                % (lo * 100, hi * 100, rlo * 100, rhi * 100)
            )
    if "analytical_ratio_max" in applied and applied["analytical_ratio_max"] > RED_LINE["analytical_ratio_max"]:
        redline.append(
            "本书旁白上限 %.0f%% 超出全局红线 %.0f%%（红线兜底警报）"
            % (applied["analytical_ratio_max"] * 100, RED_LINE["analytical_ratio_max"] * 100)
        )
    if "max_consecutive_dialogue" in applied and applied["max_consecutive_dialogue"] > RED_LINE["max_consecutive_dialogue"]:
        redline.append(
            "本书连续对话上限 %d 行 超出全局红线 %d 行（红线兜底警报）"
            % (applied["max_consecutive_dialogue"], RED_LINE["max_consecutive_dialogue"])
        )
    return out, applied, redline


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "FAIL", "reason": "缺少 JSON 参数"}, ensure_ascii=False))
        return
    raw = sys.argv[1]
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as f:
            raw = f.read()
    try:
        params = json.loads(raw)
    except Exception as e:
        print(json.dumps({"status": "FAIL", "reason": f"参数解析失败: {e}"}, ensure_ascii=False))
        return

    current_chapter = params.get("current_chapter", 0)
    target_words = params.get("target_words", 0)
    total_words = params.get("total_words", 0)   # 含本章；传入优先，缺省则自动扫描
    chapter_file = params.get("chapter_file", "")
    # —— 参数归一：mode 兼作文体模式（web/literary/cinematic）与旧续写开关（auto/manual）——
    raw_mode = str(params.get("mode", "web") or "web").strip().lower()
    auto_mode = (raw_mode == "auto")          # 旧的 "mode":"auto" 写法继续生效
    explicit_work_mode = params.get("work_mode")
    warnings = []
    if explicit_work_mode is not None:
        # 显式 work_mode 优先（0c-0 写入 state-schema 的字段名即此名）
        work_mode = str(explicit_work_mode).strip().lower()
        if work_mode not in ("web", "literary", "cinematic"):
            warnings.append(f"未知 work_mode {work_mode!r}，已回落 web")
            work_mode = "web"
    elif raw_mode in ("web", "literary", "cinematic"):
        work_mode = raw_mode
    else:
        # auto / manual 为旧续写开关写法，不算错误；其余未知值要提示
        if raw_mode not in ("auto", "manual"):
            warnings.append(f"未知 mode {raw_mode!r}，已回落 web")
        work_mode = "web"

    if not chapter_file or not os.path.exists(chapter_file):
        print(json.dumps({"status": "FAIL", "reason": f"章节文件缺失: {chapter_file}"}, ensure_ascii=False))
        return

    with open(chapter_file, "r", encoding="utf-8") as f:
        content = f.read()

    stats = analyze(content)
    chapter_han = stats["chapter_han"]

    # —— total_words：传入优先，缺省自动扫描（v1.4.4 新增）——
    if total_words and total_words > 0:
        total_source = "param"
        scanned = []
    else:
        total_words, scanned = auto_total_words(chapter_file, current_chapter)
        total_source = f"auto-scan({len(scanned)}章)" if scanned else "none"
        if not scanned:
            warnings.append("total_words 缺省且同目录无可识别的 chapter_<n>.md，跳过进度类判定")

    progress_pct = round(total_words * 100.0 / target_words, 2) if target_words else 0.0

    # —— 双栖模式阈值切换 ——
    # web（默认）：商业网文红线；literary：传统文学放宽（长铺垫、重心理、允许尽早展现结局基调）
    if work_mode == "literary":
        min_han, max_han = 2000, 8000       # 放宽上限：文学章允许长段落与长铺垫
        min_dr, max_dr = 0.05, 0.65         # 放宽台词下限至 5%、上限至 65%（允许大段对白体）
        max_analytical = 0.25               # 分析性旁白放宽至 25%（允许心理与议论）
        check_closing = False               # 取消「疑似提前收尾」拦截：允许尽早展现结局基调
    elif work_mode == "cinematic":
        # 影视化：折中档，暂沿用 web 基线，待单独标定
        min_han, max_han = MIN_HAN, MAX_HAN
        min_dr, max_dr = MIN_DIALOGUE_RATIO, MAX_DIALOGUE_RATIO
        max_analytical = MAX_ANALYTICAL_RATIO
        check_closing = True
    else:
        min_han, max_han = MIN_HAN, MAX_HAN
        min_dr, max_dr = MIN_DIALOGUE_RATIO, MAX_DIALOGUE_RATIO
        max_analytical = MAX_ANALYTICAL_RATIO
        check_closing = True

    # —— 覆盖层（本书口径）：优先级高于引擎基线，v1.4.5 ——
    base = {
        "han": [min_han, max_han],
        "dialogue_ratio": [min_dr, max_dr],
        "analytical_ratio_max": max_analytical,
        "max_consecutive_dialogue": MAX_CONSECUTIVE_DIALOGUE,
    }
    eff, ov_applied, redline_warn = apply_overrides(
        params.get("thresholds_override"), base, warnings
    )
    min_han, max_han = eff["han"]
    min_dr, max_dr = eff["dialogue_ratio"]
    max_analytical = eff["analytical_ratio_max"]
    max_consec = eff["max_consecutive_dialogue"]

    # —— 台词体可识别性（v1.4.4）——
    # 无引号体且提示语密集 → 检测器失真，不按 0% 判 FAIL，转 PAUSE 请人工确认
    dialogue_unreadable = (
        stats["dialogue_han"] == 0
        and (stats["speech_tags"] >= 4 or stats["dash_paragraphs"] >= 3)
    )

    fails = []
    if chapter_han < min_han:
        fails.append(f"汉字数 {chapter_han} < {min_han}")
    if chapter_han > max_han:
        fails.append(f"汉字数 {chapter_han} > {max_han}")
    if not dialogue_unreadable:
        if stats["dialogue_ratio"] < min_dr:
            fails.append(f"台词占比 {stats['dialogue_ratio']:.1%} < {min_dr:.0%}")
        if stats["dialogue_ratio"] > max_dr:
            fails.append(f"台词占比 {stats['dialogue_ratio']:.1%} > {max_dr:.0%}")
    if stats["analytical_ratio"] > max_analytical:
        fails.append(f"分析性旁白 {stats['analytical_ratio']:.1%} > {max_analytical:.0%}")
    if stats["max_consecutive_dialogue"] > max_consec:
        fails.append(f"连续无提示语对话 {stats['max_consecutive_dialogue']} 行 > {max_consec}")

    pauses = []
    closing_strong, closing_weak = scan_closing(content)
    # total_words=0/缺失（无累计基数）时跳过提前收尾检测，避免按 0% 误判 PAUSE
    if check_closing and target_words and total_words > 0 and progress_pct < 95:
        if closing_strong:
            pauses.append(f"疑似提前收尾（强信号）：命中 {closing_strong}")
        if closing_weak:
            pauses.append(f"疑似提前收尾（章末弱信号）：命中 {closing_weak}")
    if auto_mode and current_chapter > 0 and current_chapter % 5 == 0:
        pauses.append(f"自动续写第 {current_chapter} 章，强制暂停点")
    if dialogue_unreadable:
        pauses.append(
            "台词体未识别：全文无“”「」『』引号，但提示语 %d 处／破折号起段 %d 处，"
            "台词占比按 0%% 计会误判，请人工确认或改用引号体"
            % (stats["speech_tags"], stats["dash_paragraphs"])
        )

    details = {
        "current_chapter": current_chapter,
        "chapter_han": chapter_han,
        "dialogue_ratio": stats["dialogue_ratio"],
        "analytical_ratio": stats["analytical_ratio"],
        "analytical_soft_ratio": stats["analytical_soft_ratio"],
        "max_consecutive_dialogue": stats["max_consecutive_dialogue"],
        "quote_styles": stats["quote_styles"],
        "closing_hits": {"strong": closing_strong, "weak": closing_weak},
        "total_words": total_words,
        "total_words_source": total_source,
        "scanned_chapters": scanned,
        "target_words": target_words,
        "progress_pct": progress_pct,
        "mode": work_mode,
        "work_mode": work_mode,
        # 本次生效的阈值，便于回执核对「文学模式到底放宽了什么」
        "thresholds": {
            "han": [min_han, max_han],
            "dialogue_ratio": [min_dr, max_dr],
            "analytical_ratio_max": max_analytical,
            "closing_check": check_closing,
            "max_consecutive_dialogue": max_consec,
            "source": "overlay" if ov_applied else "engine",
            "override_applied": ov_applied,
        },
    }
    if redline_warn:
        details["red_line_warnings"] = redline_warn
    if warnings:
        details["warnings"] = warnings

    if fails:
        out = {"status": "FAIL", "reason": "；".join(fails), "details": details}
    elif pauses:
        out = {"status": "PAUSE", "reason": "；".join(pauses), "details": details}
    else:
        out = {"status": "PASS", "reason": "机械校验通过", "details": details}
    out["mode"] = work_mode   # 附带当前判定模式（web / literary / cinematic）

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
