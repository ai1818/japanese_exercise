#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
商务与求职日语实时对练私教台
=============================================================
存放位置：05_ツール/speaking_coach/jp_coach.py
特点：
1. 自动联动资料库：
   - 自动读取 06_日本語口語/个人素材.md（你自己的真实履历，不进 git）
   - 自动读取 06_日本語口語/今日推送/ 下最新一期的每日课题与 3 个思维公式模板
   - 批改记录自动沉淀至 06_日本語口語/記録/dialog_review_notes.csv
2. 零第三方包依赖：仅使用 Python 标准库，兼容 scratch 与 research 环境。
"""

import csv
import datetime
import hashlib
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 路径推导：若同级存在 data/ 目录（本机 SSD 独立部署模式），优先使用同级 data/；否则回退至 parents[1] (资料库根目录)
APP_DIR = Path(__file__).resolve().parent
_default_root = (APP_DIR / "data") if (APP_DIR / "data").is_dir() else APP_DIR.parents[1]
PROJECT_ROOT = Path(os.environ.get("JP_COACH_DATA_ROOT", str(_default_root))).resolve()
SPEAKING_DIR = PROJECT_ROOT / "06_日本語口語"
PROFILE_MD = SPEAKING_DIR / "个人素材.md"
PUSH_DIR = SPEAKING_DIR / "今日推送"
ACCUM_MD = SPEAKING_DIR / "积累库.md"
LEDGER_MD = SPEAKING_DIR / "口語訓練記録台帳.md"
RECORD_DIR = SPEAKING_DIR / "記録"

CRED_ENV_FILE = Path.home() / ".credentials" / "google" / "jp_coach.env"
LOCAL_ENV_FILE = APP_DIR / ".env"
REVIEW_CSV = (RECORD_DIR if RECORD_DIR.exists() else APP_DIR) / "dialog_review_notes.csv"  # 旧版记录（只读）
# 新版练习记录：OneDrive 同步目录下，按「日期_电脑名」分文件，多台电脑同时用也不会产生同步冲突
SESSION_DIR = (RECORD_DIR if RECORD_DIR.exists() else APP_DIR) / "coach_sessions"
MACHINE = (socket.gethostname() or "unknown").split(".")[0]

# 固定问题分类（让 Gemini 只从这里选，便于跨天统计反复出现的问题）
PROBLEM_TAGS = {
    "particle": "助词误用",
    "collocation": "动词/搭配不当",
    "keigo": "敬语变形错误",
    "register": "太随便的口语词（〜とか/〜じゃん）",
    "written": "书面语过重（不像口语）",
    "calque": "中式直译",
    "structure": "逻辑结构/PREP 缺失",
    "evidence": "缺少具体数据或例子",
    "verbose": "句子冗长/重复",
    "filler": "赘词卡顿（えーと/まあ）",
    "pronunciation": "发音（长音/促音/清浊）",
    "accent": "语调/重音",
    "vocab": "词汇不足（中英夹杂）",
}
SCORE_KEYS = {"grammar": "语法", "keigo": "敬语", "logic": "逻辑", "fluency": "流畅", "pronunciation": "发音"}


def load_config() -> dict:
    """按优先级读取配置：~/.credentials/google/jp_coach.env > 同目录 .env > 系统环境变量"""
    cfg = {
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", "").strip(),
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip(),
    }
    for env_path in (LOCAL_ENV_FILE, CRED_ENV_FILE):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "GEMINI_API_KEY" and v and not v.startswith("在这里粘贴"):
                    cfg["GEMINI_API_KEY"] = v
                elif k == "GEMINI_MODEL" and v:
                    cfg["GEMINI_MODEL"] = v
    return cfg


def save_config(api_key: str, model: str = "gemini-3.8-flash") -> None:
    CRED_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Google AI Studio (AIzaSy...) 或 Vertex AI Express (AQ.Ab8...) API Key\n"
        f"GEMINI_API_KEY={api_key.strip()}\n"
        f"GEMINI_MODEL={model.strip()}\n"
    )
    CRED_ENV_FILE.write_text(content, encoding="utf-8", newline="\n")


def load_personal_profile() -> str:
    """读取 06_日本語口語/个人素材.md 作为 AI 面试官的背景知识"""
    if PROFILE_MD.exists():
        return PROFILE_MD.read_text(encoding="utf-8", errors="ignore").strip()
    return ""


def _ledger_recorded_days() -> set:
    """检查口語訓練記録台帳.md 中已写入有效定稿（§5）的 Day 编号集合"""
    if not LEDGER_MD.exists():
        return set()
    try:
        text = LEDGER_MD.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    day_re = re.compile(r"^##\s+Day\s+(\d+)\s*[:：]")
    sec_re = re.compile(r"^###\s+(\d+)\s*[.．、]")
    days = {}
    cur_day, cur_sec = None, None
    for line in text.splitlines():
        m = day_re.match(line)
        if m:
            cur_day = int(m.group(1))
            days.setdefault(cur_day, {})
            cur_sec = None
            continue
        if line.startswith("# ") or line.startswith("## "):
            cur_day, cur_sec = None, None
            continue
        if cur_day is None:
            continue
        s = sec_re.match(line)
        if s:
            cur_sec = int(s.group(1))
            days[cur_day].setdefault(cur_sec, [])
            continue
        if cur_sec is not None:
            days[cur_day][cur_sec].append(line)
    recorded = set()
    for d_num, secs in days.items():
        body5 = " ".join(l.strip().lstrip(">").strip() for l in secs.get(5, []) if l.strip() and l.strip() != "---")
        if body5 and "待填入" not in body5:
            recorded.add(d_num)
    return recorded


def parse_daily_push_md(md_text: str, filename: str = "") -> dict:
    """从今日推送 Markdown 镜像（及同目录 latest_lesson.json）解析结构化字段，供前端零 Gemini 开场使用"""
    info = {
        "filename": filename,
        "content": md_text,
        "day": None,
        "date": filename.replace(".md", "") if filename.endswith(".md") else "",
        "theme": "",
        "difficulty": "",
        "question_ja": "",
        "question_zh": "",
        "templates": [],
        "answer_template": {},
        "tip": "",
        "skeleton_cn": {},
        "locked": False,
    }
    if not md_text:
        return info

    m_head = re.search(r"^#\s+Day\s+(\d+)\s*[・·]\s*(\d{4}-\d{2}-\d{2})\s*[・·]\s*(.+)$", md_text, re.M)
    if m_head:
        info["day"] = int(m_head.group(1))
        info["date"] = m_head.group(2).strip()
        info["theme"] = m_head.group(3).strip()

    m_diff = re.search(r"^-\s*难度[:：]\s*(.+)$", md_text, re.M)
    if m_diff:
        info["difficulty"] = m_diff.group(1).strip()

    # 提取 3 个公式模板（及可选的子字段：假名标注、详细讲解、示范例句、例句中译、举例讲解）
    tpl_blocks = re.split(r"(?=^\s*\d+\.\s*`[^`]+`)", md_text, flags=re.M)
    for blk in tpl_blocks:
        m_tpl = re.match(r"^\s*\d+\.\s*`([^`]+)`(.*)$", blk, re.M)
        if not m_tpl:
            continue
        item = {
            "template": m_tpl.group(1).strip(),
            "meta": m_tpl.group(2).strip(),
            "template_furigana": "",
            "detail_cn": "",
            "example_ja": "",
            "example_zh": "",
            "example_note_cn": "",
        }
        for label, key in (
            ("假名标注", "template_furigana"),
            ("详细讲解", "detail_cn"),
            ("示范例句", "example_ja"),
            ("例句中译", "example_zh"),
            ("举例讲解", "example_note_cn"),
        ):
            m_sub = re.search(rf"^\s*-\s*\*\*{label}\*\*[:：]\s*(.+)$", blk, re.M)
            if m_sub:
                item[key] = m_sub.group(1).strip()
        info["templates"].append(item)

    # 提取面试官提问
    m_q = re.search(
        r"###\s*2\.\s*面试官提问场景[\s\S]*?>\s*`「([^」]+)」`\s*\n>\s*[（(]([^\n）)]+)[）)]",
        md_text,
    )
    if m_q:
        info["question_ja"] = m_q.group(1).strip()
        info["question_zh"] = m_q.group(2).strip()
    else:
        m_q2 = re.search(r">\s*`「([^」]+)」`", md_text)
        if m_q2:
            info["question_ja"] = m_q2.group(1).strip()

    m_tip = re.search(r"^提示[:：]\s*(.+)$", md_text, re.M)
    if m_tip:
        info["tip"] = m_tip.group(1).strip()

    # 提取 §3 完整回答模板
    m_skel = re.search(r"-\s*\*\*填空开口骨架[^*]*\*\*[:：]\s*\n\s*>\s*(.+)", md_text)
    m_mod_ja = re.search(r"-\s*\*\*满分口语示范回答[^*]*\*\*[:：]\s*\n\s*>\s*(.+)", md_text)
    m_mod_zh = re.search(r"-\s*\*\*示范回答中文释义[^*]*\*\*[:：]\s*\n\s*>\s*(.+)", md_text)
    if m_skel or m_mod_ja:
        mod_ja = m_mod_ja.group(1).strip() if m_mod_ja else ""
        info["answer_template"] = {
            "skeleton_ja": m_skel.group(1).strip() if m_skel else "",
            "model_ja": mod_ja,
            "model_clean_ja": _clean_tts_text(mod_ja),
            "model_zh": m_mod_zh.group(1).strip() if m_mod_zh else "",
        }

    # 提取中文母逻辑 P / R+E / P
    m_p1 = re.search(r"^\s*-\s*P[:：]\s*(.+)$", md_text, re.M)
    m_re = re.search(r"^\s*-\s*R\+E[:：]\s*(.+)$", md_text, re.M)
    all_p = re.findall(r"^\s*-\s*P[:：]\s*(.+)$", md_text, re.M)
    if m_p1:
        info["skeleton_cn"]["p"] = m_p1.strip() if isinstance(m_p1, str) else m_p1.group(1).strip()
    if m_re:
        info["skeleton_cn"]["re"] = m_re.group(1).strip()
    if len(all_p) >= 2:
        info["skeleton_cn"]["p2"] = all_p[1].strip()

    # 若存在 latest_lesson.json 且 Day 一致，补充更完整的结构化字段（含 name/detail_cn/example_ja/answer_template）
    latest_json = PUSH_DIR / "latest_lesson.json"
    if latest_json.exists():
        try:
            lj = json.loads(latest_json.read_text(encoding="utf-8"))
            if not info["day"] or lj.get("day") == info["day"]:
                info["day"] = lj.get("day", info["day"])
                info["theme"] = lj.get("theme", info["theme"])
                info["question_ja"] = lj.get("question_ja", info["question_ja"])
                info["question_zh"] = lj.get("question_zh", info["question_zh"])
                info["tip"] = lj.get("tip", info["tip"])
                if lj.get("answer_template"):
                    info["answer_template"] = lj["answer_template"]
                lj_tmps = lj.get("templates") or []
                if len(lj_tmps) == len(info["templates"]):
                    for idx, lt in enumerate(lj_tmps):
                        for k in ("name", "timing_tense", "function", "template_furigana", "detail_cn", "example_ja", "example_zh", "example_note_cn"):
                            if lt.get(k) and not info["templates"][idx].get(k):
                                info["templates"][idx][k] = lt[k]
                elif lj_tmps and not info["templates"]:
                    info["templates"] = lj_tmps
        except Exception:
            pass

    if info["day"]:
        info["locked"] = info["day"] in _ledger_recorded_days()
    return info


def load_latest_daily_push() -> dict:
    """读取 06_日本語口語/今日推送/ 下最新一期的每日口语卡片并解析结构化字段"""
    if not PUSH_DIR.exists():
        return parse_daily_push_md("", "")
    md_files = sorted(PUSH_DIR.glob("*.md"), reverse=True)
    if not md_files:
        return parse_daily_push_md("", "")
    latest = md_files[0]
    raw = latest.read_text(encoding="utf-8", errors="ignore").strip()
    return parse_daily_push_md(raw, latest.name)


def lock_daily_to_ledger(payload: dict) -> dict:
    """将用户在网页私教台满意的批改定稿写入 06_日本語口語/口語訓練記録台帳.md，解锁明日下一课"""
    latest = load_latest_daily_push()
    day_n = int(payload.get("day") or latest.get("day") or (max(_ledger_recorded_days(), default=7) + 1))
    theme = (payload.get("theme") or latest.get("theme") or "科研与商务面试专项").strip()
    push_date = (payload.get("date") or latest.get("date") or datetime.date.today().isoformat()).strip()
    rewrite_jp = (payload.get("rewrite_jp") or "").strip()
    if not rewrite_jp:
        raise ValueError("定稿内容为空，请先在私教台完成至少一轮回答批改。")

    user_transcript = (payload.get("user_transcript") or "（私教台直接锁定定稿）").strip()
    error_breakdown = payload.get("error_breakdown") or []
    unclear_pron = payload.get("unclear_pronunciations") or []
    critique_cn = (payload.get("critique_cn") or "").strip()
    scores = payload.get("scores") or {}

    # 组装 §1 当日推送要素
    tpl_lines = ["### 1. 当日推送要素（3 个公式模板）", "- **思维公式模板**："]
    if latest.get("templates"):
        for idx, t in enumerate(latest["templates"], 1):
            meta = f" {t['meta']}" if t.get("meta") else ""
            tpl_lines.append(f"  {idx}. `{t['template']}`{meta}")
    else:
        tpl_lines.append("  1. `（见当日邮件推送卡片）`")

    # 组装 §2 面试官提问场景
    q_ja = latest.get("question_ja") or payload.get("question_ja") or "（当日面试官提问）"
    q_zh = latest.get("question_zh") or payload.get("question_zh") or ""
    q_lines = [
        "### 2. 面试官提问场景",
        "> **面接官**：",
        f"> `「{q_ja}」`",
    ]
    if q_zh:
        q_lines.append(f"> （{q_zh}）")

    # 组装 §3 原始即兴转录文本
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    s3_lines = [
        "### 3. 原始即兴转录文本（私教台网页端实战录音）",
        f"- **记录时间**：`{now_str}`（本机 `{MACHINE}`）",
        "```text",
        user_transcript,
        "```",
    ]

    # 组装 §4 四维诊断与关键纠偏记录（注意避开 ledger.py 的 _SKIP_BLOCKS 标题，以便次日早报提取纠偏要点）
    s4_lines = ["### 4. 四维诊断与关键纠偏记录"]
    if error_breakdown:
        s4_lines.append("- **语法与表达纠偏**：")
        for err in error_breakdown:
            orig = (err.get("original_part") or "").replace("❌", "").strip()
            fixed = (err.get("fixed_part") or "").replace("✅", "").strip()
            reason = (err.get("reason_cn") or "").replace("💡", "").strip()
            if orig or fixed:
                s4_lines.append(f"  - `{orig}` → **`{fixed}`**（{reason}）")
    if unclear_pron:
        s4_lines.append("- **发音与咬字微调**：")
        for u in unclear_pron:
            heard = (u.get("heard_as") or "").strip()
            target = (u.get("target_word") or "").strip()
            tip = (u.get("tip_cn") or "").strip()
            if heard or target:
                s4_lines.append(f"  - 误听 `{heard}` → 应读作 **`{target}`**（{tip}）")
    if critique_cn:
        s4_lines.append("- **结构与改进总结**：")
        for line in critique_cn.splitlines():
            line = line.strip()
            if line:
                s4_lines.append(f"  - {line}")
    if not error_breakdown and not unclear_pron and not critique_cn:
        s4_lines.append("- **表达与结构复盘**：")
        s4_lines.append("  - 完整运用当日 3 个思维公式完成 PREP 回答。")

    # 组装 §5 最终交付版本
    s5_lines = [
        "### 5. 最终交付版本（带平假名注音・自然面接対話版）",
        "",
    ]
    for para in rewrite_jp.splitlines():
        para = para.strip()
        if para:
            s5_lines.append(f"> {para}")

    # 组装 §6 肌肉记忆与最终达成状态（含 ★ 供 ledger.py 识别为已锁定）
    score_str = " / ".join(f"{SCORE_KEYS.get(k, k)} {v}" for k, v in scores.items()) if scores else "私教台精批通过"
    s6_lines = [
        "### 6. 肌肉记忆与最终达成状态",
        f"- **私教台网页端评分**：{score_str}",
        "- **达成状态**：★ 100% 肌肉记忆锁定（已由私教台网页端一键锁定，可供晨间脚本自动回溯并推进至下一课）",
    ]

    section_md = "\n".join([
        f"## Day {day_n}: {theme} ({push_date})",
        "",
        *tpl_lines,
        "",
        *q_lines,
        "",
        *s3_lines,
        "",
        *s4_lines,
        "",
        *s5_lines,
        "",
        *s6_lines,
        "",
        "---",
        "",
    ])

    LEDGER_MD.parent.mkdir(parents=True, exist_ok=True)
    existing = LEDGER_MD.read_text(encoding="utf-8", errors="ignore") if LEDGER_MD.exists() else "# 日语口语与商务面试表达训练台账（デイリーログ）\n\n---\n\n"

    # 若已有 ## Day N: 则原地更新，否则追加到末尾并在目录索引补充链接
    pattern = re.compile(
        rf"^##\s+Day\s+{day_n}\s*[:：].*?(?=^##\s+Day\s+\d+\s*[:：]|\Z)",
        re.M | re.S,
    )
    if pattern.search(existing):
        updated = pattern.sub(section_md.rstrip() + "\n\n", existing, count=1)
    else:
        toc_line = f"- [Day {day_n}: {theme} ({push_date})](#day-{day_n}-{theme}-{push_date})"
        if "## 目录索引" in existing and f"[Day {day_n}:" not in existing:
            existing = re.sub(
                r"(## 目录索引\n(?:- \[.*?\]\(.*?\)\n)+)",
                lambda m: m.group(1) + toc_line + "\n",
                existing,
                count=1,
            )
        updated = existing.rstrip() + "\n\n---\n\n" + section_md

    tmp = LEDGER_MD.with_suffix(".md.tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, LEDGER_MD)
    _trigger_bg_sync()
    return {
        "ok": True,
        "day": day_n,
        "next_day": day_n + 1,
        "theme": theme,
        "date": push_date,
        "ledger_path": str(LEDGER_MD),
    }


def _trigger_bg_sync() -> None:
    """本地 SSD 模式下发生数据写入后，异步非阻塞触发一次后台 OneDrive 同步（OneDrive 故障时静默忽略）"""
    sync_script = APP_DIR / "sync_onedrive.py"
    if not sync_script.exists():
        return
    try:
        import subprocess
        subprocess.Popen(
            [sys.executable, str(sync_script), "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def append_to_accum_md(jp_expr: str, cn_note: str) -> str:
    """将地道表达追加写入 06_日本語口語/积累库.md（遵循 `- 表达｜备注` 格式）"""
    ACCUM_MD.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {jp_expr.strip()}｜{cn_note.strip()}\n"
    if not ACCUM_MD.exists():
        ACCUM_MD.write_text("# 日语口语积累库\n\n## 整句 / 缓冲话术\n" + line, encoding="utf-8")
    else:
        with ACCUM_MD.open("a", encoding="utf-8") as f:
            f.write(line)
    _trigger_bg_sync()
    return str(ACCUM_MD)


def append_review_note(row: dict) -> None:
    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    file_exists = REVIEW_CSV.exists()
    fieldnames = [
        "timestamp",
        "scenario",
        "user_original",
        "polite_colleague_jp",
        "executive_keigo_jp",
        "critique_cn",
        "key_phrases",
    ]
    with REVIEW_CSV.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


def append_session_record(record: dict) -> Path:
    """每次批改写一行 JSON 到 coach_sessions/YYYY-MM-DD_<电脑名>.jsonl"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    day = record["timestamp"][:10]
    path = SESSION_DIR / f"{day}_{MACHINE}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trigger_bg_sync()
    return path


def _clean_scores(raw) -> dict:
    out = {}
    if isinstance(raw, dict):
        for k in SCORE_KEYS:
            v = raw.get(k)
            if isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    continue
            if isinstance(v, (int, float)) and 1 <= v <= 10:
                out[k] = round(float(v), 1)
    return out


def load_all_records() -> list:
    """汇总所有电脑的练习记录（新版 jsonl + 旧版 csv），按时间排序"""
    records = []
    if SESSION_DIR.exists():
        for fp in sorted(SESSION_DIR.glob("*.jsonl")):
            for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if REVIEW_CSV.exists():
        with REVIEW_CSV.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                records.append({
                    "timestamp": row.get("timestamp", ""),
                    "machine": "(旧版记录)",
                    "scenario": row.get("scenario", ""),
                    "question": "",
                    "input_type": "",
                    "user_transcript": row.get("user_original", ""),
                    "scores": {},
                    "problem_tags": [],
                    "error_breakdown": [],
                    "critique_cn": row.get("critique_cn", ""),
                    "executive_keigo_rewrite": row.get("executive_keigo_jp", ""),
                    "legacy": True,
                })
    records.sort(key=lambda r: r.get("timestamp", ""))
    return records


SYSTEM_PROMPT_TEMPLATE = """あなたは日本のトップ企業・研究機関で活躍する面接官・R&D部長であり、同時に〔CANDIDATE〕向けの「面接で自然に話せる日本語（話し言葉）と発音のコーチ」です。

【候補者（ユーザー）の実際のバックグラウンド（06_日本語口語/个人素材.md より）】
{personal_profile}

【現在のロールプレイ設定】
{scenario_desc}

【現在の練習モード】
{mode_instruction}

【最重要：口語スタイル原則と「自己矛盾の完全禁止」（すべての書き換え・模範解答・添削に適用）】
ユーザーが練習しているのは「日本の技術面接で実際に口に出して自然に話す丁寧な日本語」です。
1. 1文は短く（目安40〜50字以内）、1文に1つの内容。「〜し、〜し、〜であり、」と長くつながない。
2. 面接で自然に使える丁寧な話し言葉を基準にする：
   - ✕ 避けるべき極端な書き言葉・公文調：「〜にございます／〜にございました」「〜てまいる所存です」「遺憾なく発揮」「〜および」「貴社（口頭では御社）」
   - ○ 推奨する面接の自然な敬体：「〜でした」「〜に貢献したいと考えています」「〜に取り組みました／挑戦しました／検証しました」「中心になって進めました／主導しました」「御社（おんしゃ）」「〜をもとに／〜を踏まえて」
3. 敬語は「です・ます」が基本。謙譲語「申します／いたします／させていただきます」は自然に使ってよい（1回答に2〜3回まで可。「志望いたしました」「検証いたしました」は面接で自然なので直さない）。二重敬語や「〜強くございます」のような不自然な接客調だけ直す。
4. 【絶対厳守：自己矛盾の禁止（アンチ・フリップフロップ原則）】
   - ユーザーが「直前のターンであなた（コーチ）が提示した `executive_keigo_rewrite` / `polite_colleague_rewrite` / `next_answer_template`」や「今日推送の公式テンプレート・模範解答」の表現を参考にして話した場合、**その表現を「書き言葉すぎる」「硬すぎる」「くだけすぎ／幼稚」と批判して別の言い方に変えさせることは絶対禁止**です！
   - 例：前のターンであなたが「安い材料」「文献調べ」「挑戦しました」「強くございます」等を提示していた場合、またはユーザーが「安価な材料」「文献調査」「主導し」「検証いたしました」等の自然な技術面接表現を使った場合、それを error_breakdown で減点・修正してはいけません。
   - 2回目以降の言い直し（リトライ）では、ユーザーが前回の添削スクリプトをほぼ再現できていれば `error_breakdown` は空配列 `[]`（または本当に残っている文法ミス・助詞落ちのみ）にし、語彙の好みの揺れで毎回違う書き換えを押し付けないでください。
   - `polite_colleague_rewrite`（カジュアル面談・同僚ディスカッション版）でも、技術面接で減点されるようなくだけすぎた語（「安い」「文献調べ」等）は使わず、「安価な」「文献調査」等の自然な技術用語に統一してください。

【あなたのタスク】
ユーザーが音声またはテキストで発言します（日本語の途中で分からない単語を中国語や英語で混ぜて話す場合もあります）。
音声入力の場合は、音声の「発音の明瞭度・長音/促音/濁音のズレ・言い淀み（えーと/まあ/あの）・イントネーション（語尾の伸びや不自然な高低）」も注意深く聴き取って診断してください。
必ず以下のJSONスキーマに厳密に従って応答を返してください（マークダウンのコードブロックは不要、純粋なJSONのみ）：

{{
  "user_transcript": "ユーザーの発言の正確な書き起こし（言い淀みやフィラー『えーと』『まあ』、発音が崩れて聞き取りづらかった箇所もそのまま忠実に記録する）",
  "has_improvement": true,
  "pronunciation_prosody": {{
    "overall_fluency_cn": "用简体中文慢速、温和但具体地点评本次语音的【语速节奏、停顿卡壳位置、赘词（えーと/まあ等）、句尾语调自然度】。若是纯文字输入则写『当前为文字输入；使用麦克风录音或上传音频时，此处会为您诊断咬字、停顿与语调高低』。若是第二次重说同一题，请明确表扬比上一遍进步的地方！",
    "unclear_pronunciations": [
      {{
        "heard_as": "实际听感或容易被误听成的词（如：減少（げんしょう） / 深まえで）",
        "target_word": "正确目标词与假名（如：検証（けんしょう） / 踏（ふ）まえて）",
        "tip_cn": "发音口型、清浊音送气、长音/促音节拍的具体纠正建议"
      }}
    ],
    "pitch_accent_guide": [
      {{
        "word": "本句核心词（带假名，如：検証（けんしょう））",
        "clean_word": "検証",
        "accent_pattern": "け↗んしょう→ [0] 平板型（低・高・高・高）",
        "tip_cn": "首拍低，第2拍起保持平高，接助词『を』时不下降"
      }}
    ]
  }},
  "error_breakdown": [
    {{
      "original_part": "❌ 你原话中的问题片段",
      "fixed_part": "✅ 建议修改为（带假名标注）",
      "reason_cn": "💡 问题诊断与修改意见（指出是助词搭配错误、敬语变形错误、太随便的口语词、书面语过重不像口语、中式直译，还是缺少PREP逻辑衔接）"
    }}
  ],
  "polite_colleague_rewrite": "カジュアル面談・社内の技術ディスカッションで話す版。です・ます調の自然な話し言葉、短い文で（難読漢字にはカッコでひらがなを付す）",
  "polite_colleague_clean": "polite_colleague_rewriteからカッコ内のふりがなを除いた音声読み上げ用テキスト",
  "executive_keigo_rewrite": "面接本番（役員面接を含む）でそのまま口に出せる版。丁寧だが話し言葉で、PREPの順に短い文で。謙譲語は口語スタイル原則3の範囲に留め、書き言葉の硬い表現は使わない（難読漢字にはカッコでひらがなを付す）",
  "executive_keigo_clean": "executive_keigo_rewriteからカッコ内のふりがなを除いた音声読み上げ用テキスト",
  "critique_cn": "用简体中文总结整体修改思路（1. 结构逻辑是否符合PREP；2. 若处于『今日推送』模式，是否自然套用了当天的3个思维公式模板；3. 下一次重说本句时最该注意的1个重点）。",
  "key_vocab": [
    {{
      "jp": "核心商务/就活惯用词或短语（带假名读音）",
      "clean_jp": "音声読み上げ用の純粋な日本語表記",
      "cn": "中文精准释义与语境说明"
    }}
  ],
  "assessment": {{
    "scores": {{
      "grammar": "1〜10の数値（助詞・活用・語法の正確さ）",
      "keigo": "1〜10の数値（敬語・語体の適切さ）",
      "logic": "1〜10の数値（PREP構造・論理展開・具体性）",
      "fluency": "1〜10の数値（言い淀み・停頓・話速の自然さ。文字入力なら文章の流れで判断）",
      "pronunciation": "1〜10の数値。音声入力の場合のみ。文字入力なら null"
    }},
    "problem_tags": ["今回の発言で実際に見られた問題のみ、次のIDから最大4つ選ぶ（IDの英字のみを書く）: {problem_tag_ids}"],
    "one_line_progress_cn": "用一句简体中文概括本次表现（若是重说同一题，写明和上一遍相比哪里进步、哪里还没改）"
  }},
  "reply_jp": "面接官・コーチとしての返答（練習モードの指示に従うこと。自然なビジネス日本語、ゆっくり聞き取りやすい2〜3文程度）",
  "reply_reading_hint": "reply_jpの中で聴き取りにくい難読語やビジネス表現にカッコでひらがな読みを補ったテキスト",
  "reply_cn": "reply_jp の簡体字中国語訳",
  "next_answer_template": {{
    "p_conclusion": "【P・結論（第1文の型）】いま投げかけた質問（または現在練習中の質問）に対して、ユーザーがそのまま穴埋めして話し始められる開口テンプレート",
    "re_evidence": "【R/E・理由と具体例（第2〜3文の型）】ユーザーの実際の研究（〔RESEARCH_HINT〕）をどう当てはめるかの穴埋め骨格",
    "p_closing": "【P・結びと貢献（最終文の型）】話を綺麗に締めるテンプレート",
    "model_answer_jp": "【模範解答フルスクリプト（ふりがな付き）】45秒程度で話せる自然な満点回答例。口語スタイル原則を厳守し、実際の面接で人が話すリズムの短い文で（難読漢字にはカッコでひらがなを付す）",
    "model_answer_clean_jp": "model_answer_jpからカッコ内のふりがなを除いた純粋な日本語テキスト（音声読み上げ用）"
  }}
}}
"""

SCENARIOS = {
    "daily_push": (
        "【今日推送・深掘り面接モード】\n"
        "以下の「本日の課題（3つの思考公式テンプレート＋完全回答テンプレート＋面接官の質問）」に基づき、まずは記載されている面接官の質問から会話を始め、"
        "ユーザーが回答したら3つの公式テンプレート（P→R/E→P の論理骨格）を自然に使いこなせているか添削しつつ、面接官としてさらに深掘りしてください。\n"
        "【重要：テンプレートと添削の統一】\n"
        "1. ユーザーが本日の3つの公式テンプレートや完全回答テンプレート、あるいは直前のターンであなたが提示した書き換え案（`executive_keigo_rewrite` / `polite_colleague_rewrite`）の表現を使って回答した場合、それを『書き言葉すぎる』『硬すぎる』『暗記っぽい』と批判してはいけません！テンプレート通りに言えていれば大いに褒めてください。\n"
        "2. 逆に、旧来の硬い表現（『〜にございました』『〜てまいる所存です』『貴社』）がテンプレート側に残っていた場合のみ、減点せずに『口頭では「〜でした」「〜に貢献したいと考えています」「御社」と言うとさらに自然ですよ』と優しく補足してください。\n\n"
    ),
    "chem_rd": (
        "あなたは日本のメーカーのR&D部門の技術面接官です。"
        "候補者の研究内容（個人素材に書かれた範囲）について、社会実装・スケールアップ時の課題・コスト意識を鋭く深掘り質問してください。"
    ),
    "it_ai_ds": (
        "あなたはテック・AI企業の技術面接官です。"
        "候補者の研究でのデータ解析・自動化の経験について、モデリングの工夫、データ駆動での課題解決力、なぜこの領域に挑戦したいのかを深掘りしてください。"
    ),
    "quant_finance": (
        "あなたは金融機関の面接官です。"
        "市場の動き、リスク管理、候補者の分析経験について議論してください。"
    ),
    "custom": (
        "ユーザーが指定したトピックや貼り付けた企業別資料に基づき、日本の面接官・ビジネスパートナーとして自然な敬語で議論を深めてください。"
    ),
}


GEMINI_TIMEOUT = 150
MAX_ATTEMPTS = 2
ERROR_LOG = APP_DIR / "coach_errors.log"


class _Retryable(Exception):
    """Gemini 返回了，但内容不能用（空/JSON 截断），值得原样再请求一次"""


def log_error(context: str, err: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{stamp}] [{MACHINE}] {context}\n{err}\n"
    print(msg, file=sys.stderr, flush=True)
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def gemini_generate_json(api_key: str, model: str, payload: dict) -> dict:
    """调用 Gemini 并把返回解析成 JSON；含超时、重试、换通道逻辑（所有功能共用）"""
    if not api_key:
        raise ValueError("尚未配置 GEMINI_API_KEY，请在练习台右上角填入密钥。")
    q_key = urllib.parse.quote(api_key)
    q_model = urllib.parse.quote(model)
    aistudio_url = f"https://generativelanguage.googleapis.com/v1beta/models/{q_model}:generateContent?key={q_key}"
    vertex_url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{q_model}:generateContent?key={q_key}"

    endpoints = [
        ("Gemini API (已激活密钥)", aistudio_url),
        ("Vertex AI Express", vertex_url),
    ]

    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    errors = []

    def _one_call(ep_name: str, url: str) -> dict:
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        # 录音批改常常要 40~90 秒，45 秒会被误判为失败
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
        candidates = resp_json.get("candidates", [])
        if not candidates:
            raise _Retryable(f"返回空 candidates: {str(resp_json)[:300]}")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        raw_text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not raw_text.strip():
            raise _Retryable(f"返回内容为空（finishReason={cand.get('finishReason')}）")
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise _Retryable(f"返回的 JSON 不完整（finishReason={cand.get('finishReason')}）: {e}")
        result["_endpoint"] = ep_name
        return result

    for ep_name, url in endpoints:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return _one_call(ep_name, url)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                errors.append(f"[{ep_name} 第{attempt}次 HTTP {e.code}]: {err_body[:300]}")
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                    time.sleep(3 * attempt)  # 服务器繁忙/限流：稍等再试同一通道
                    continue
                if e.code in (429, 500, 502, 503, 504):
                    raise RuntimeError("Gemini 服务器暂时繁忙，已自动重试仍失败：\n" + "\n".join(errors))
                break  # 400/401/403/404：这个通道不认这把 Key，换下一个通道
            except _Retryable as e:
                errors.append(f"[{ep_name} 第{attempt}次]: {e}")
                if attempt < MAX_ATTEMPTS:
                    continue
                raise RuntimeError("Gemini 返回内容异常，已自动重试仍失败：\n" + "\n".join(errors))
            except (TimeoutError, socket.timeout) as e:
                errors.append(f"[{ep_name}] 超过 {GEMINI_TIMEOUT} 秒未响应: {e}")
                # 超时说明通道是通的、只是慢；换通道没有意义，直接报错
                raise RuntimeError("Gemini 响应超时（网络慢或录音太长）：\n" + "\n".join(errors))
            except urllib.error.URLError as e:
                if isinstance(e.reason, (TimeoutError, socket.timeout)):
                    errors.append(f"[{ep_name}] 连接超时: {e.reason}")
                    raise RuntimeError("连接 Gemini 超时，请检查网络/代理：\n" + "\n".join(errors))
                errors.append(f"[{ep_name} 第{attempt}次 网络错误]: {e.reason}")
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2)
                    continue
                raise RuntimeError("连不上 Gemini（网络或代理问题）：\n" + "\n".join(errors))
            except Exception as e:
                errors.append(f"[{ep_name} 第{attempt}次 {type(e).__name__}]: {e}")
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2)
                    continue
                break

    raise RuntimeError("API 调用失败：\n" + "\n".join(errors))


def call_gemini_api(
    api_key: str,
    model: str,
    scenario_key: str,
    custom_context: str,
    history: list,
    user_text: str = "",
    audio_b64: str = "",
    audio_mime: str = "audio/webm",
    drill_same: bool = True,
) -> dict:
    if not api_key:
        raise ValueError("尚未配置 GEMINI_API_KEY，请在页面右上角或 .env 文件中填入密钥。")

    profile_text = load_personal_profile()
    scenario_desc = SCENARIOS.get(scenario_key, SCENARIOS["chem_rd"])
    if scenario_key == "daily_push":
        push_info = load_latest_daily_push()
        scenario_desc += push_info.get("content", "（今日推送ファイルが見つかりません）")
    if custom_context.strip():
        scenario_desc += f"\n\n【追加の背景資料・トピック】\n{custom_context.strip()}"

    if drill_same:
        mode_instruction = (
            "現在は【1問じっくり反復練習モード（単題死磕モード）】です。\n"
            "ユーザーが回答した際、急いで次の新しい質問に進まないでください！\n"
            "reply_jpでは、ユーザーの回答の良い点を1文で認めた上で、「今の質問について、右側の添削スクリプトや発音アドバイスを参考に、もう一度ゆっくり言い直してみましょう（満足したら『次の質問へ』を押してください）」と優しく促してください。"
            "next_answer_templateには、いま練習しているこの質問に対するブラッシュアップされた穴埋めテンプレートと模範解答を提示してください。"
        )
    else:
        mode_instruction = (
            "現在は【連続深掘り面接モード】です。reply_jpではユーザーの発言内容に踏み込んで返答し、次の深掘り質問を1つ投げかけてください。"
        )

    system_instruction = SYSTEM_PROMPT_TEMPLATE.format(
        personal_profile=profile_text or "（プロフィール未読込）",
        scenario_desc=scenario_desc,
        mode_instruction=mode_instruction,
        problem_tag_ids=", ".join(f"{k}({v})" for k, v in PROBLEM_TAGS.items()),
    )

    contents = []
    for turn in history[-8:]:
        role = "user" if turn.get("role") == "user" else "model"
        text = turn.get("text", "")
        if role == "model" and turn.get("rewrites"):
            text += (
                f"\n\n【※システム記録：このターンであなた（コーチ）がユーザーに提示した模範書き換え】\n"
                f"・面接本番版: {turn['rewrites'].get('exec', '')}\n"
                f"・面談版: {turn['rewrites'].get('polite', '')}\n"
                f"（※次のユーザー発言がこの模範書き換えに沿っている場合、あなた自身が推奨した表現を『硬すぎる』『くだけすぎ』等と自己矛盾して批判することは厳禁です。前回の書き換え案を可能な限り維持し、発音や残りの明確な文法ミスのみチェックしてください）"
            )
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})

    user_parts = []
    if audio_b64:
        user_parts.append({"inlineData": {"mimeType": audio_mime, "data": audio_b64}})
        prompt_suffix = (
            "添付の音声は私の発言です。まず正確に文字起こしを行い、ビジネス日本語の添削と、"
            "面接官としての深掘り返答をJSON形式で出力してください。"
        )
        if user_text.strip():
            prompt_suffix += f"\n補足テキスト: {user_text.strip()}"
        user_parts.append({"text": prompt_suffix})
    else:
        user_parts.append({"text": user_text.strip() or "面接を始めてください。最初の質問をお願いします。"})

    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
        },
    }

    return gemini_generate_json(api_key, model, payload)


TTS_CACHE_DIR = PROJECT_ROOT / "tts_cache"
VOICE_MAP = {
    "female": "ja-JP-NanamiNeural",
    "nanami": "ja-JP-NanamiNeural",
    "staff": "ja-JP-NanamiNeural",
    "stylist": "ja-JP-NanamiNeural",
    "officer": "ja-JP-NanamiNeural",
    "male": "ja-JP-KeitaNeural",
    "keita": "ja-JP-KeitaNeural",
    "doctor": "ja-JP-KeitaNeural",
    "driver": "ja-JP-KeitaNeural",
    "manager": "ja-JP-KeitaNeural",
    "me": "ja-JP-KeitaNeural",
}


def _clean_tts_text(text: str) -> str:
    """朗读前去除括号内的注音假名、背景舞台说明（如（点検後）（ピンポーン）（または…））及方括号符号，防止把背景说明或注音念出来"""
    t = re.sub(r"（[^）]*）|\([^)]*\)", "", text or "")
    t = re.sub(r"[【\[][^】\]]*[】\]]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def synthesize_cloud_tts(api_key: str, text: str, voice_name: str = "ja-JP-NanamiNeural", speaking_rate: float = 1.0) -> str:
    """使用微软 Edge Neural TTS (Nanami / Keita) 生成真人级日语语音，缓存在本地 SSD，完全不调用 Gemini。"""
    import base64
    clean_text = _clean_tts_text(text)
    if not clean_text:
        return ""
    resolved_voice = VOICE_MAP.get((voice_name or "").strip().lower(), voice_name or "ja-JP-NanamiNeural")
    if not resolved_voice.startswith("ja-JP-") or "Neural2" in resolved_voice:
        resolved_voice = "ja-JP-NanamiNeural"

    try:
        pct = max(-50, min(50, int(round((float(speaking_rate) - 1.0) * 100))))
    except (TypeError, ValueError):
        pct = 0
    rate_str = f"{pct:+d}%"

    cache_key = hashlib.sha1(f"edge_v1|{resolved_voice}|{rate_str}|{clean_text}".encode("utf-8")).hexdigest()
    cache_fp = TTS_CACHE_DIR / f"{cache_key}.mp3"
    try:
        if cache_fp.exists() and cache_fp.stat().st_size > 100:
            return base64.b64encode(cache_fp.read_bytes()).decode("ascii")
    except OSError:
        pass

    try:
        import asyncio
        import edge_tts

        async def _gen() -> bytes:
            comm = edge_tts.Communicate(clean_text, resolved_voice, rate=rate_str)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        mp3_bytes = asyncio.run(_gen())
        if mp3_bytes:
            try:
                TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp_fp = cache_fp.with_suffix(".mp3.tmp")
                tmp_fp.write_bytes(mp3_bytes)
                os.replace(tmp_fp, cache_fp)
            except OSError:
                pass
            return base64.b64encode(mp3_bytes).decode("ascii")
    except Exception as e:
        log_error("edge_tts", str(e))

    # 兜底：若未来配置了 Google Cloud TTS 专用 Key
    if not api_key:
        return ""
    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={urllib.parse.quote(api_key)}"
    payload = {
        "input": {"text": clean_text},
        "voice": {"languageCode": "ja-JP", "name": "ja-JP-Neural2-C"},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": speaking_rate},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("audioContent", "")
    except Exception:
        return ""


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日语口语私教台</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f172a">
<meta name="apple-mobile-web-app-title" content="口语私教台">
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --border: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --success: #34d399;
    --warning: #fbbf24;
    --danger: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", "Meiryo", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 16px 22px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 14px;
    flex-wrap: wrap;
    gap: 12px;
  }
  .title-box h1 { font-size: 1.2rem; font-weight: 700; color: #fff; }
  .title-box span { font-size: 0.82rem; color: var(--muted); }
  .api-bar {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  input, select, textarea, button {
    font-family: inherit;
    font-size: 0.88rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: #0b1120;
    color: var(--text);
    padding: 6px 10px;
  }
  button {
    cursor: pointer;
    background: #2563eb;
    border-color: #3b82f6;
    font-weight: 600;
    transition: all 0.15s;
  }
  button:hover { background: #1d4ed8; }
  button.secondary { background: var(--panel); border-color: var(--border); }
  button.secondary:hover { background: #334155; }
  .badge {
    font-size: 0.76rem;
    padding: 3px 8px;
    border-radius: 99px;
    background: #064e3b;
    color: var(--success);
    border: 1px solid #059669;
  }
  .layout {
    display: grid;
    grid-template-columns: 1.15fr 1fr;
    gap: 16px;
    height: calc(100vh - 105px);
    min-height: 600px;
  }
  @media (max-width: 860px) {
    .layout { grid-template-columns: 1fr; height: auto; min-height: 0; }
    .card { max-height: 78vh; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 15px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 10px;
  }
  .card-header h2 { font-size: 1rem; color: var(--accent); }
  .push-banner {
    background: #0b1120;
    border: 1px solid #1e40af;
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 10px;
    font-size: 0.84rem;
    max-height: 150px;
    overflow-y: auto;
    white-space: pre-wrap;
    color: #cbd5e1;
  }
  .chat-scroll, .review-scroll {
    flex: 1;
    overflow-y: auto;
    padding-right: 6px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .bubble {
    padding: 12px 15px;
    border-radius: 8px;
    background: #0f172a;
    border: 1px solid var(--border);
  }
  .bubble.user {
    border-left: 4px solid var(--accent);
    background: #172554;
  }
  .bubble.ai {
    border-left: 4px solid var(--success);
  }
  .bubble-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.78rem;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .jp-text {
    font-size: 1.06rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    transition: filter 0.25s ease;
  }
  .blurred {
    filter: blur(7px);
    user-select: none;
    cursor: pointer;
  }
  .blurred:hover { filter: blur(4px); }
  .hint-box {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--border);
    font-size: 0.88rem;
    color: #cbd5e1;
    display: none;
  }
  .hint-box.visible { display: block; }
  .controls-area {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .input-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }
  .input-row textarea {
    flex: 1;
    resize: none;
    height: 60px;
  }
  .mic-btn {
    width: 128px;
    background: #dc2626;
    border-color: #ef4444;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }
  .mic-btn:hover { background: #b91c1c; }
  .mic-btn.recording {
    background: #991b1b;
    animation: pulse 1.2s infinite;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.6); }
    70% { box-shadow: 0 0 0 12px rgba(239, 68, 68, 0); }
    100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
  }
  .critique-block {
    background: #0f172a;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 13px;
  }
  .critique-block h3 {
    font-size: 0.83rem;
    color: var(--muted);
    margin-bottom: 4px;
  }
  .rewrite-jp {
    font-size: 1.01rem;
    color: #fff;
    background: #1e293b;
    padding: 8px 11px;
    border-radius: 6px;
    border-left: 3px solid var(--warning);
    margin: 5px 0 9px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .rewrite-jp.exec { border-left-color: var(--success); }
  .vocab-item {
    background: #1e293b;
    padding: 7px 10px;
    border-radius: 6px;
    margin-top: 6px;
    font-size: 0.88rem;
  }
  .small-btn {
    padding: 3px 8px;
    font-size: 0.76rem;
    white-space: nowrap;
  }
</style>
</head>
<body>

<header>
  <div class="title-box">
    <h1>商务与求职日语实时私教台</h1>
    <span>已自动挂载：06_日本語口語/个人素材.md & 今日推送 · 错题自动存至 06_日本語口語/記録/</span>
  </div>
  <div class="api-bar">
    <a href="/bank" style="color:#0b1120; background:var(--accent); padding:6px 12px; border-radius:6px; font-weight:700; font-size:0.86rem; text-decoration:none;">📚 面试题库</a>
    <a href="/scenes" style="color:#0b1120; background:#34d399; padding:6px 12px; border-radius:6px; font-weight:700; font-size:0.86rem; text-decoration:none;">🗣 生活场景</a>
    <a href="/progress" target="_blank" style="color:#0b1120; background:var(--success); padding:6px 12px; border-radius:6px; font-weight:700; font-size:0.86rem; text-decoration:none;">📈 我的进步 & 问题</a>
    <span id="epBadge" class="badge">正在检测密钥...</span>
    <input type="password" id="apiKeyInput" placeholder="粘贴新的 API Key 可随时替换" style="width: 200px;">
    <select id="modelSelect">
      <option value="gemini-3.8-flash">gemini-3.8-flash (最新极速推荐)</option>
      <option value="gemini-flash-latest">gemini-flash-latest</option>
      <option value="gemini-pro-latest">gemini-pro-latest (深度精批)</option>
      <option value="gemini-3.1-pro-preview">gemini-3.1-pro-preview</option>
    </select>
    <button class="secondary" onclick="saveApiKey()">保存配置</button>
  </div>
</header>

<div class="layout">
  <div class="card">
    <div class="card-header">
      <h2>1. 实战面试与慢速精练区（默认毛玻璃盲听，点击或按 Tab 显字）</h2>
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <label style="font-size:0.8rem; color:#34d399; font-weight:600;" title="勾选后，每次你说完，教练会先慢速纠错并让你重说本题，不会急着跳到下一题">
          <input type="checkbox" id="drillSameToggle" checked> 🎯 单题死磕模式（先纠错重说）
        </label>
        <label style="font-size:0.8rem; color:var(--muted);">
          <input type="checkbox" id="blindModeToggle" checked> 盲听遮罩
        </label>
        <select id="speedSelect" style="padding:3px 6px; font-size:0.8rem;">
          <option value="0.65">语速 0.65x (极慢逐字)</option>
          <option value="0.80" selected>语速 0.80x (慢速推荐)</option>
          <option value="0.90">语速 0.90x (适中)</option>
          <option value="1.0">语速 1.0x (母语原速)</option>
        </select>
        <button class="small-btn secondary" onclick="stopAllAudio()" title="立即中断当前正在播放的语音">⏹ 停止播放</button>
      </div>
    </div>

    <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap;">
      <select id="scenarioSelect" style="flex:1;" onchange="onScenarioChange()">
        <option value="daily_push">🔥 场景 0：06_日本語口語「今日推送」实战对练（套用当日 3 个公式模板 + 追问）</option>
        <option value="chem_rd">〔LABEL_chem_rd〕</option>
        <option value="it_ai_ds">〔LABEL_it_ai_ds〕</option>
        <option value="quant_finance">〔LABEL_quant_finance〕</option>
        <option value="custom">📝 场景 D：自定义企业/研报对谈（可粘贴 02_企業別 笔记或财报段落）</option>
      </select>
      <button onclick="startOpeningQuestion()">1️⃣ 让面试官提问</button>
      <button style="background:#059669; border-color:#10b981;" onclick="askNextQuestion()">➡️ 练熟了，追问下一题</button>
      <button class="secondary" onclick="clearConversation()">清空重来</button>
    </div>

    <div id="pushBanner" class="push-banner" style="display:none;"></div>
    <textarea id="customContext" placeholder="（可选）在此粘贴你想针对性练习的企业 JD、ES 段落或日文研报..." style="height:48px; margin-bottom:10px; display:none;"></textarea>

    <div id="chatScroll" class="chat-scroll">
      <div class="bubble ai">
        <div class="bubble-meta"><span>系统就绪（默认开启「0.80x 慢速 + 单题死磕精批模式」）</span></div>
        <div style="font-size:0.93rem; color:#cbd5e1;">
          <b>推荐训练节奏（不要急着一直往下聊，一题说顺再过关）：</b><br>
          ① 点击上方 <b>「1️⃣ 让面试官提问」</b> → 听问题并看下方 <b>填空提词板</b>；<br>
          ② 点击下方红色 <b>「点击录音说话」</b> 回答第 1 遍；<br>
          ③ 看右侧 <b>「🎵 语音语调诊断 + ❌ 逐处错误修改意见」</b>，点 🔊 慢速跟读后，<b>直接再按录音键重说第 2 遍</b>；<br>
          ④ 觉得这题彻底说顺了，再点上方绿色 <b>「➡️ 练熟了，追问下一题」</b>。
        </div>
      </div>
    </div>

    <div class="controls-area">
      <div class="input-row">
        <button id="micBtn" class="mic-btn" onclick="toggleRecording()">
          <span id="micIcon" style="font-size:1.3rem;">🎙️</span>
          <span id="micLabel">点击录音说话</span>
        </button>
        <button class="secondary" style="display:flex; flex-direction:column; align-items:center; justify-content:center; padding:0 10px; font-size:0.78rem;" onclick="document.getElementById('audioFileInput').click()" title="上传手机或电脑上的 .m4a / .mp3 / .wav 录音文件">
          <span style="font-size:1.1rem;">📂</span>
          <span>传录音档</span>
        </button>
        <input type="file" id="audioFileInput" accept="audio/*,.m4a,.mp3,.wav,.webm" style="display:none;" onchange="handleAudioFileUpload(event)">
        <textarea id="userInput" placeholder="也可以直接打字输入日语（或日中混杂），按 Ctrl+Enter 发送..."></textarea>
        <button onclick="sendText()" style="padding:0 18px;">发送</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header" style="padding-bottom:6px; margin-bottom:6px;">
      <h2>2. 手术刀式口语重构 & 实时纠音</h2>
      <span style="font-size:0.74rem; color:var(--muted);">自动记录至 06_日本語口語/記録/</span>
    </div>

    <details style="background:#0b1120; border:1px solid #1e40af; border-radius:6px; padding:5px 10px; margin-bottom:8px; font-size:0.78rem; max-height:135px; overflow-y:auto; flex-shrink:0;">
      <summary style="cursor:pointer; font-weight:600; color:var(--accent);">🧰 开口提词模板 & 核心积木（卡壳时点此展开）</summary>
      <div style="margin-top:6px; display:flex; flex-direction:column; gap:4px; color:#cbd5e1; line-height:1.45;">
        <div><b>① 接球缓冲：</b><code>ご質問ありがとうございます。結論から言いますと、【核心結論】です。</code>
          <button class="small-btn secondary" onclick="speakJapanese('ご質問ありがとうございます。結論から言いますと、')">🔊</button>
        </div>
        <div><b>② 引出痛点（R）：</b><code>【表面課題】だけではなくて、本当に難しかったのは【真のボトルネック】のほうでした。</code>
          <button class="small-btn secondary" onclick="speakJapanese('それだけではなくて、本当に難しかったのは、ボトルネックのほうでした。')">🔊</button>
        </div>
        <div><b>③ 个人积木（E）：</b><br>〔BLOCKS_E〕
        </div>
        <div><b>④ 收尾扣题（P）：</b><code>この経験で身につけた【材料からプロセスまでやり切る力／データで課題を解く力】を、御社でも活かしたいと考えています。</code>
          <button class="small-btn secondary" onclick="speakJapanese('この経験で身につけた、材料からプロセスまでやり切る力を、御社でも活かしたいと考えています。')">🔊</button>
        </div>
      </div>
    </details>

    <div id="reviewScroll" class="review-scroll">
      <div class="critique-block">
        <h3>等待你的第一句日语发言...</h3>
        <p style="font-size:0.88rem; color:var(--muted); margin-top:6px;">
          每次发言后，这里会实时生成：<br>
          1. <b>你的原话识别</b><br>
          2. <b>面谈/技术讨论版（です・ます 口语）</b><br>
          3. <b>面试本番版（礼貌但口语，可直接说出口）</b><br>
          点击句子右侧的 <b>「🔊 跟读」</b> 即可反复听标准发音并影子跟读。
        </p>
      </div>
    </div>
  </div>
</div>

<script>
let chatHistory = [];
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let latestPushData = null;

async function initConfig() {
  const res = await fetch('/api/config');
  const data = await res.json();
  const badge = document.getElementById('epBadge');
  if (data.has_key) {
    badge.textContent = `已就绪 (${data.masked_key})`;
    badge.style.background = '#064e3b';
  } else {
    badge.textContent = '未配置 Key';
    badge.style.background = '#7f1d1d';
  }
  document.getElementById('modelSelect').value = data.model || 'gemini-3.8-flash';
  latestPushData = data.latest_push;
  const urlScenario = new URLSearchParams(window.location.search).get('scenario');
  if (urlScenario && document.querySelector(`#scenarioSelect option[value="${urlScenario}"]`)) {
    document.getElementById('scenarioSelect').value = urlScenario;
  }
  onScenarioChange();
}

async function saveApiKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  const model = document.getElementById('modelSelect').value;
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key, model: model})
  });
  const data = await res.json();
  alert(data.message);
  document.getElementById('apiKeyInput').value = '';
  initConfig();
}

function renderPushBanner() {
  const val = document.getElementById('scenarioSelect').value;
  const banner = document.getElementById('pushBanner');
  if (val === 'daily_push' && latestPushData && latestPushData.content) {
    banner.style.display = 'block';
    const dNum = latestPushData.day || '?';
    const lockBadge = latestPushData.locked
      ? `<span style="background:#064e3b;color:#34d399;border:1px solid #059669;padding:1px 8px;border-radius:99px;font-size:0.75rem;">✅ Day ${dNum} 已锁定进台账（明日推 Day ${dNum + 1}）</span>`
      : `<span style="background:#451a03;color:#fbbf24;border:1px solid #d97706;padding:1px 8px;border-radius:99px;font-size:0.75rem;">⏳ Day ${dNum} 待锁定定稿（练完点右侧批改卡底部「✅ 锁定今日定稿」推进明日新课）</span>`;
    const tplItems = (latestPushData.templates || []).map((t, i) => `
      <div style="margin:4px 0; background:#111c35; padding:6px 9px; border-radius:6px; border-left:3px solid #3b82f6;">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
          <span><b>公式 ${i+1}${t.name ? ' · ' + escapeHtml(t.name) : ''}：</b><code style="color:#93c5fd;">${escapeHtml(t.template_furigana || t.template)}</code></span>
          <button class="small-btn secondary" onclick="speakJapanese('${escapeJs((t.template || '').replace(/【[^】]+】/g, ''))}')">🔊 听句型</button>
        </div>
        ${t.detail_cn ? `<div style="margin-top:3px; color:#cbd5e1; font-size:0.76rem;">🔍 <b>讲解：</b>${escapeHtml(t.detail_cn)}</div>` : ''}
        ${t.example_ja ? `<div style="margin-top:3px; color:#6ee7b7; font-size:0.76rem; display:flex; justify-content:space-between; align-items:center; gap:6px;">
          <span>💡 <b>例句：</b>${escapeHtml(t.example_ja)} <span style="color:#94a3b8;">（${escapeHtml(t.example_zh || '')}）</span></span>
          <button class="small-btn secondary" onclick="speakJapanese('${escapeJs(t.example_ja)}')">🔊 读例句</button>
        </div>` : ''}
        ${t.example_note_cn ? `<div style="margin-top:2px; color:#fbbf24; font-size:0.74rem;">🧩 <b>填槽拆解：</b>${escapeHtml(t.example_note_cn)}</div>` : ''}
      </div>
    `).join('');
    banner.innerHTML = `
      <details>
        <summary style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; font-weight:600; color:#93c5fd;">
          <span>📬 今日邮件课题：Day ${escapeHtml(dNum)} · ${escapeHtml(latestPushData.theme || latestPushData.filename)} (${escapeHtml(latestPushData.date || '')}) — 点此展开 3 大公式精讲与例句</span>
          ${lockBadge}
        </summary>
        <div style="margin-top:6px; font-size:0.78rem; line-height:1.45;">
          ${tplItems}
          ${latestPushData.tip ? `<div style="margin-top:4px; color:#fbbf24;">💡 ${escapeHtml(latestPushData.tip)}</div>` : ''}
        </div>
      </details>
    `;
  } else {
    banner.style.display = 'none';
  }
}

function renderDailyPushOpening(playAudio = false) {
  if (!latestPushData || !latestPushData.question_ja) return false;
  const chatScroll = document.getElementById('chatScroll');
  chatHistory = [{ role: 'model', text: latestPushData.question_ja }];
  const blindClass = document.getElementById('blindModeToggle').checked ? 'blurred' : '';
  const msgId = 'msg-daily-' + Date.now();
  const safeQ = escapeJs(latestPushData.question_ja);
  const sk = latestPushData.skeleton_cn || {};
  const tpls = latestPushData.templates || [];
  const at = latestPushData.answer_template || {};

  const detailedTplsHtml = tpls.map((t, idx) => `
    <div style="margin-bottom:8px; padding:8px 10px; background:#0f172a; border:1px solid #1e3a8a; border-left:3px solid #38bdf8; border-radius:6px;">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
        <div>
          <span style="font-weight:700; color:#38bdf8;">公式 ${idx+1}${t.name ? '（' + escapeHtml(t.name) + '）' : ''}</span>
          ${t.timing_tense ? `<span style="font-size:0.73rem; background:#1e3a8a; color:#93c5fd; padding:1px 6px; border-radius:99px; margin-left:4px;">${escapeHtml(t.timing_tense)}</span>` : ''}
          <div style="margin-top:4px; font-size:0.9rem; color:#f8fafc; font-weight:600; line-height:1.55;">${escapeHtml(t.template_furigana || t.template)}</div>
        </div>
        <button class="small-btn secondary" onclick="speakJapanese('${escapeJs((t.template || '').replace(/【[^】]+】/g, ''))}')">🔊 听骨架</button>
      </div>
      ${t.function ? `<div style="margin-top:4px; font-size:0.79rem; color:#cbd5e1;">🎯 <b>功能：</b>${escapeHtml(t.function)}</div>` : ''}
      ${t.detail_cn ? `<div style="margin-top:3px; font-size:0.79rem; color:#cbd5e1; line-height:1.5;">🔍 <b>语法与口语精讲：</b>${escapeHtml(t.detail_cn)}</div>` : ''}
      ${t.example_ja ? `
        <div style="margin-top:5px; padding-top:5px; border-top:1px dashed #334155; font-size:0.8rem;">
          <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:6px;">
            <span style="color:#6ee7b7; line-height:1.55;">💡 <b>示范例句：</b>${escapeHtml(t.example_ja)}</span>
            <button class="small-btn secondary" onclick="speakJapanese('${escapeJs(t.example_ja)}')">🔊 朗读例句</button>
          </div>
          ${t.example_zh ? `<div style="color:#94a3b8; font-size:0.76rem; margin-top:2px;">中译：${escapeHtml(t.example_zh)}</div>` : ''}
          ${t.example_note_cn ? `<div style="color:#fbbf24; font-size:0.76rem; margin-top:2px;">🧩 <b>填槽举例讲解：</b>${escapeHtml(t.example_note_cn)}</div>` : ''}
        </div>
      ` : ''}
    </div>
  `).join('');

  const fullAnswerHtml = (at.skeleton_ja || at.model_ja) ? `
    <div style="margin-top:10px; padding:10px 12px; background:#064e3b33; border:1px solid #059669; border-radius:8px; font-size:0.84rem;">
      <div style="font-weight:700; color:#34d399; margin-bottom:6px;">🌟 今日提问·完整回答模板（串联 3 个公式，可直接填空或照读 · 与 AI 批改标准 100% 统一）：</div>
      ${at.skeleton_ja ? `
        <div style="margin-bottom:8px;">
          <div style="font-size:0.78rem; color:#a7f3d0; font-weight:600; margin-bottom:3px;">① 填空回答骨架（带假名注音，把【 】换成你的词直接开口）：</div>
          <div style="background:#0f172a; padding:7px 10px; border-radius:6px; color:#e2e8f0; line-height:1.65;">${escapeHtml(at.skeleton_ja)}</div>
        </div>
      ` : ''}
      ${at.model_ja ? `
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:3px;">
            <span style="font-size:0.78rem; color:#6ee7b7; font-weight:600;">② 满分口语示范回答（结合你的个人素材填好，带假名注音，跟读此版绝不会被批“太书面”）：</span>
            <div style="display:flex; gap:6px;">
              <button class="small-btn" style="background:#059669; border-color:#10b981;" onclick="speakJapanese('${escapeJs(at.model_clean_ja || at.model_ja)}')">🔊 朗读完整示范</button>
              <button class="small-btn secondary" onclick="saveToAccum('${escapeJs(at.model_clean_ja || at.model_ja)}', 'Day ${escapeJs(latestPushData.day || '')} 满分示范回答', this)">➕ 存入积累库</button>
            </div>
          </div>
          <div style="background:#0f172a; padding:8px 10px; border-radius:6px; color:#f8fafc; line-height:1.7; font-size:0.88rem;">${escapeHtml(at.model_ja)}</div>
          ${at.model_zh ? `<div style="color:#94a3b8; font-size:0.77rem; margin-top:4px;"><b>【中文释义】</b>${escapeHtml(at.model_zh)}</div>` : ''}
        </div>
      ` : ''}
    </div>
  ` : '';

  const tplHtml = (tpls.length || sk.p) ? `
    <div style="margin-top:10px; padding:10px 12px; background:#111c35; border:1px solid #1d4ed8; border-radius:8px; font-size:0.84rem;">
      <div style="font-weight:700; color:#60a5fa; margin-bottom:8px;">📋 今日 3 个思维公式精讲（含假名标注・详细讲解・举例拆解 · 0 额外 Gemini 消耗）：</div>
      ${detailedTplsHtml}
      ${sk.p ? `<div style="margin-top:6px; padding-top:6px; border-top:1px dashed #334155; font-size:0.79rem; color:#cbd5e1;">
        <div><b>🧠 中文母逻辑 P：</b>${escapeHtml(sk.p)}</div>
        ${sk.re ? `<div><b>🧠 R+E：</b>${escapeHtml(sk.re)}</div>` : ''}
        ${sk.p2 ? `<div><b>🧠 P：</b>${escapeHtml(sk.p2)}</div>` : ''}
      </div>` : ''}
    </div>
    ${fullAnswerHtml}
  ` : '';
  chatScroll.innerHTML = `
    <div class="bubble ai">
      <div class="bubble-meta">
        <span>🎯 今日邮件面试官提问（Day ${escapeHtml(latestPushData.day || '')} · ${escapeHtml(latestPushData.theme || '')}）</span>
        <div style="display:flex; gap:6px;">
          <button class="small-btn" onclick="speakJapanese('${safeQ}')">🔊 朗读提问</button>
          <button class="small-btn secondary" onclick="toggleReveal('${msgId}')">👁️ 显字/译文</button>
        </div>
      </div>
      <div id="${msgId}-jp" class="jp-text ${blindClass}" onclick="toggleReveal('${msgId}')">${escapeHtml(latestPushData.question_ja)}</div>
      <div id="${msgId}-hint" class="hint-box visible">
        <div style="color:#94a3b8;"><b>【中文译文】</b> ${escapeHtml(latestPushData.question_zh || '')}</div>
      </div>
      ${tplHtml}
    </div>
  `;
  if (playAudio) {
    speakJapanese(latestPushData.question_ja);
  }
  return true;
}

function onScenarioChange() {
  const val = document.getElementById('scenarioSelect').value;
  document.getElementById('customContext').style.display = (val === 'custom') ? 'block' : 'none';
  renderPushBanner();
  if (val === 'daily_push' && chatHistory.length === 0) {
    renderDailyPushOpening(false);
  }
}

function clearConversation() {
  chatHistory = [];
  document.getElementById('chatScroll').innerHTML = '';
  if (document.getElementById('scenarioSelect').value === 'daily_push') {
    renderDailyPushOpening(false);
  }
}

const ttsCache = {};
let curAudio = null;
function stopAllAudio() {
  if (curAudio) {
    try { curAudio.pause(); curAudio.currentTime = 0; } catch (e) {}
    curAudio = null;
  }
  if ('speechSynthesis' in window) {
    try { speechSynthesis.cancel(); } catch (e) {}
  }
}
async function speakJapanese(text, b64Mp3 = "", voice = "keita") {
  if (!text) return;
  stopAllAudio();
  const rate = parseFloat(document.getElementById('speedSelect').value || '0.9');
  if (b64Mp3) {
    const audio = new Audio("data:audio/mp3;base64," + b64Mp3);
    curAudio = audio;
    audio.onended = () => { if (curAudio === audio) curAudio = null; };
    audio.play().catch(() => {});
    return;
  }
  const cacheKey = `${voice}|${rate}|${text}`;
  if (!(cacheKey in ttsCache)) {
    try {
      const res = await fetch('/api/tts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ text, rate, voice })
      });
      const data = await res.json();
      ttsCache[cacheKey] = data.b64 || '';
    } catch (e) {
      ttsCache[cacheKey] = '';
    }
  }
  if (ttsCache[cacheKey]) {
    const audio = new Audio("data:audio/mp3;base64," + ttsCache[cacheKey]);
    curAudio = audio;
    audio.onended = () => { if (curAudio === audio) curAudio = null; };
    audio.play().catch(() => {});
    return;
  }
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = 'ja-JP';
  utter.rate = rate;
  const voices = window.speechSynthesis.getVoices().filter(v => v.lang.includes('ja') || v.lang.includes('JP'));
  const preferred = voices.find(v => v.name.includes('Natural') || v.name.includes('Google') || v.name.includes('Nanami') || v.name.includes('Keita')) || voices[0];
  if (preferred) utter.voice = preferred;
  window.speechSynthesis.speak(utter);
}

if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
}

async function toggleRecording() {
  const btn = document.getElementById('micBtn');
  const label = document.getElementById('micLabel');
  if (!isRecording) {
    stopAllAudio();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];
      mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
      mediaRecorder.onstop = async () => {
        const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
        stream.getTracks().forEach(t => t.stop());
        await sendAudioBlob(blob, mediaRecorder.mimeType || 'audio/webm');
      };
      mediaRecorder.start();
      isRecording = true;
      btn.classList.add('recording');
      label.textContent = '正在听...再点发送';
    } catch (err) {
      alert('无法访问麦克风，请检查浏览器麦克风权限：' + err.message);
    }
  } else {
    mediaRecorder.stop();
    isRecording = false;
    btn.classList.remove('recording');
    label.textContent = '点击录音说话';
  }
}

async function sendAudioBlob(blob, mimeType) {
  const reader = new FileReader();
  reader.onloadend = async () => {
    const base64data = reader.result.split(',')[1];
    await dispatchChat({ audio_b64: base64data, audio_mime: mimeType, user_text: document.getElementById('userInput').value });
    document.getElementById('userInput').value = '';
  };
  reader.readAsDataURL(blob);
}

async function handleAudioFileUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  let mime = file.type || 'audio/mp4';
  if (file.name.endsWith('.m4a')) mime = 'audio/mp4';
  else if (file.name.endsWith('.mp3')) mime = 'audio/mp3';
  else if (file.name.endsWith('.wav')) mime = 'audio/wav';
  await sendAudioBlob(file, mime);
  event.target.value = '';
}

async function sendText() {
  const txt = document.getElementById('userInput').value.trim();
  if (!txt) return;
  document.getElementById('userInput').value = '';
  await dispatchChat({ user_text: txt });
}

async function startOpeningQuestion() {
  if (document.getElementById('scenarioSelect').value === 'daily_push' && latestPushData && latestPushData.question_ja) {
    renderDailyPushOpening(true);
    return;
  }
  await dispatchChat({
    user_text: "（面接セッション開始：設定されたシナリオに基づき、面接官として最初の質問をゆっくり明瞭なビジネス日本語で投げかけてください）",
    is_opening: true,
    drill_same: false
  });
}

async function askNextQuestion() {
  await dispatchChat({
    user_text: "（今の質問の反復練習は完了しました。私のこれまでの回答を踏まえて、面接官として次の深掘り質問、または新しい面接質問を1つ投げかけてください）",
    is_opening: true,
    drill_same: false
  });
}

const pendingRetries = {};
function showChatError(msg, payload) {
  const id = 'err-' + Date.now();
  pendingRetries[id] = payload;  // 保留原来的录音/文字，重试时不用重录
  const hasAudio = payload && payload.audio_b64;
  document.getElementById('chatScroll').insertAdjacentHTML('beforeend', `
    <div id="${id}" class="bubble ai" style="border-left-color:var(--danger);">
      <div class="bubble-meta"><span style="color:var(--danger);">⚠️ 这次调用 Gemini 失败${hasAudio ? '（录音已保留）' : ''}</span>
        <button class="small-btn" onclick="retryChat('${id}')">🔁 原样重试</button></div>
      <div style="font-size:0.82rem; color:#fca5a5; white-space:pre-wrap; max-height:140px; overflow:auto;">${escapeHtml(msg)}</div>
    </div>`);
  const cs = document.getElementById('chatScroll');
  cs.scrollTop = cs.scrollHeight;
}
function retryChat(id) {
  const payload = pendingRetries[id];
  delete pendingRetries[id];
  const el = document.getElementById(id);
  if (el) el.remove();
  if (payload) dispatchChat(payload);
}

async function dispatchChat(payload) {
  const chatScroll = document.getElementById('chatScroll');
  const tempId = 'loading-' + Date.now();
  chatScroll.insertAdjacentHTML('beforeend', `
    <div id="${tempId}" class="bubble ai">
      <div class="bubble-meta"><span>AI 教练正在慢速听音与逐句批改...</span></div>
      <div style="color:var(--muted);">⏳ 正在诊断你的语音停顿、声调高低、语法错误并生成修改意见...</div>
    </div>
  `);
  chatScroll.scrollTop = chatScroll.scrollHeight;

  try {
    const drillSame = (payload.drill_same !== undefined) ? payload.drill_same : document.getElementById('drillSameToggle').checked;
    const reqBody = {
      scenario: document.getElementById('scenarioSelect').value,
      custom_context: document.getElementById('customContext').value,
      model: document.getElementById('modelSelect').value,
      history: chatHistory,
      drill_same: drillSame,
      ...payload
    };
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(reqBody)
    });
    const data = await res.json();
    document.getElementById(tempId).remove();

    if (!res.ok || data.error) {
      showChatError(data.error || ('HTTP ' + res.status), payload);
      return;
    }

    if (data._endpoint) {
      document.getElementById('epBadge').textContent = `通道: ${data._endpoint}`;
    }

    const r = data.result;
    if (!payload.is_opening && r.user_transcript) {
      chatHistory.push({ role: 'user', text: r.user_transcript });
      chatScroll.insertAdjacentHTML('beforeend', `
        <div class="bubble user">
          <div class="bubble-meta"><span>你的发言识别</span></div>
          <div class="jp-text">${escapeHtml(r.user_transcript)}</div>
        </div>
      `);
      renderCritique(r);
    }

    chatHistory.push({
      role: 'model',
      text: r.reply_jp,
      rewrites: {
        exec: r.executive_keigo_rewrite || '',
        polite: r.polite_colleague_rewrite || ''
      }
    });
    const blindClass = document.getElementById('blindModeToggle').checked ? 'blurred' : '';
    const msgId = 'msg-' + Date.now();
    const safeReply = escapeJs(r.reply_jp);
    const safeMp3 = r.tts_b64 || '';
    const tpl = r.next_answer_template || {};
    const tplHtml = tpl.p_conclusion ? `
      <div style="margin-top:10px; padding:10px 12px; background:#111c35; border:1px solid #1d4ed8; border-radius:8px; font-size:0.86rem;">
        <div style="font-weight:700; color:#60a5fa; margin-bottom:6px;">📋 本问回答填空提词板（看着骨架直接开口填词）：</div>
        <div style="margin-bottom:4px; color:#e2e8f0;"><b>1. [P 結論]</b> ${escapeHtml(tpl.p_conclusion)}</div>
        <div style="margin-bottom:4px; color:#e2e8f0;"><b>2. [R/E 根拠]</b> ${escapeHtml(tpl.re_evidence || '')}</div>
        <div style="margin-bottom:6px; color:#e2e8f0;"><b>3. [P 結び]</b> ${escapeHtml(tpl.p_closing || '')}</div>
        ${tpl.model_answer_jp ? `
          <details style="margin-top:6px; border-top:1px dashed #334155; padding-top:6px;">
            <summary style="cursor:pointer; color:#34d399; font-weight:600;">🌟 实在卡壳？点此查看本题满分示范回答 & 听发音跟读</summary>
            <div style="margin-top:6px; color:#f1f5f9; line-height:1.65;">${escapeHtml(tpl.model_answer_jp)}</div>
            <div style="margin-top:6px; display:flex; gap:8px;">
              <button class="small-btn" onclick="speakJapanese('${escapeJs(tpl.model_answer_clean_jp || tpl.model_answer_jp)}')">🔊 朗读满分示范</button>
              <button class="small-btn secondary" onclick="saveToAccum('${escapeJs(tpl.model_answer_clean_jp || tpl.model_answer_jp)}', '面试满分示范回答', this)">➕ 存入积累库</button>
            </div>
          </details>
        ` : ''}
      </div>
    ` : '';

    chatScroll.insertAdjacentHTML('beforeend', `
      <div class="bubble ai">
        <div class="bubble-meta">
          <span>对方回应（点击模糊文字可显字 / 按 Tab 键切换）</span>
          <div style="display:flex; gap:6px;">
            <button class="small-btn" onclick="speakJapanese('${safeReply}', '${safeMp3}')">🔊 重播语音</button>
            <button class="small-btn secondary" onclick="toggleReveal('${msgId}')">👁️ 显字/译文</button>
          </div>
        </div>
        <div id="${msgId}-jp" class="jp-text ${blindClass}" onclick="toggleReveal('${msgId}')">${escapeHtml(r.reply_jp)}</div>
        <div id="${msgId}-hint" class="hint-box">
          <div style="color:#38bdf8; margin-bottom:4px;"><b>【假名标注】</b> ${escapeHtml(r.reply_reading_hint || r.reply_jp)}</div>
          <div style="color:#94a3b8;"><b>【中文译文】</b> ${escapeHtml(r.reply_cn || '')}</div>
        </div>
        ${tplHtml}
      </div>
    `);
    chatScroll.scrollTop = chatScroll.scrollHeight;
    speakJapanese(r.reply_jp, safeMp3);
  } catch (e) {
    const el = document.getElementById(tempId);
    if (el) el.remove();
    showChatError('请求异常: ' + e.message + '（如果是“Failed to fetch”，说明本地服务已关闭，请重新双击 start_coach 启动）', payload);
  }
}

const SCORE_LABELS = {grammar:'语法', keigo:'敬语', logic:'逻辑', fluency:'流畅', pronunciation:'发音'};
function scoreChips(r) {
  const a = r.assessment || {};
  const sc = a.scores || {};
  const chips = Object.keys(SCORE_LABELS).filter(k => typeof sc[k] === 'number').map(k => {
    const v = sc[k]; const c = v >= 8 ? 'var(--success)' : v >= 6 ? 'var(--warning)' : 'var(--danger)';
    return `<span style="border:1px solid ${c}; color:${c}; border-radius:99px; padding:1px 8px; font-size:0.78rem;">${SCORE_LABELS[k]} ${v}</span>`;
  }).join(' ');
  if (!chips && !a.one_line_progress_cn) return '';
  return `<div style="display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px;">${chips}
    <span style="font-size:0.8rem; color:#cbd5e1;">${escapeHtml(a.one_line_progress_cn || '')}</span>
    <span style="font-size:0.72rem; color:var(--muted); margin-left:auto;">✔ 已存入进步记录</span></div>`;
}

const critiquePayloads = {};

function renderCritique(r) {
  const reviewScroll = document.getElementById('reviewScroll');
  const cardId = 'crit-' + Date.now();
  critiquePayloads[cardId] = r;
  const prosody = r.pronunciation_prosody || {};
  const unclearList = (prosody.unclear_pronunciations || []).map(u => `
    <div class="vocab-item" style="border-left:3px solid var(--danger);">
      <div><b>听感偏误：</b><span style="color:var(--danger);">${escapeHtml(u.heard_as)}</span> → <b>应读作：</b><span style="color:var(--success);">${escapeHtml(u.target_word)}</span></div>
      <div style="color:#cbd5e1; font-size:0.83rem; margin-top:2px;">💡 咬字纠正：${escapeHtml(u.tip_cn)}</div>
    </div>
  `).join('');

  const pitchList = (prosody.pitch_accent_guide || []).map(p => `
    <div class="vocab-item" style="display:flex; justify-content:space-between; align-items:center; gap:8px; border-left:3px solid var(--accent);">
      <div>
        <b>${escapeHtml(p.word)}</b> ：<code style="color:#fbbf24;">${escapeHtml(p.accent_pattern)}</code>
        <div style="color:#94a3b8; font-size:0.8rem;">${escapeHtml(p.tip_cn || '')}</div>
      </div>
      <button class="small-btn secondary" onclick="speakJapanese('${escapeJs(p.clean_word || p.word)}')">🔊 听音调</button>
    </div>
  `).join('');

  const errList = (r.error_breakdown || []).map(e => `
    <div class="vocab-item" style="border-left:3px solid var(--warning);">
      <div style="color:#fca5a5;">${escapeHtml(e.original_part)}</div>
      <div style="color:#6ee7b7; font-weight:600; margin-top:2px;">${escapeHtml(e.fixed_part)}</div>
      <div style="color:#cbd5e1; font-size:0.83rem; margin-top:3px;">${escapeHtml(e.reason_cn)}</div>
    </div>
  `).join('');

  const vocabHtml = (r.key_vocab || []).map(v => `
    <div class="vocab-item" style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
      <span><b style="color:var(--accent);">${escapeHtml(v.jp)}</b> — ${escapeHtml(v.cn)}</span>
      <div style="display:flex; gap:4px;">
        <button class="small-btn secondary" onclick="speakJapanese('${escapeJs(v.clean_jp || v.jp)}')">🔊</button>
        <button class="small-btn secondary" onclick="saveToAccum('${escapeJs(v.jp)}', '${escapeJs(v.cn)}', this)">➕ 积累库</button>
      </div>
    </div>
  `).join('');

  const politeClean = r.polite_colleague_clean || r.polite_colleague_rewrite || '';
  const execClean = r.executive_keigo_clean || r.executive_keigo_rewrite || '';
  const dayNum = (latestPushData && latestPushData.day) ? latestPushData.day : '';

  const cardHtml = `
    <div class="critique-block">
      ${scoreChips(r)}
      <h3>1. 你的原话忠实转写（含卡壳与口头禅）</h3>
      <div style="color:#cbd5e1; margin-bottom:10px; background:#1e293b; padding:8px 10px; border-radius:6px;">${escapeHtml(r.user_transcript || '')}</div>

      <h3>2. 🎵 语音・语调・停顿诊断</h3>
      <div style="font-size:0.88rem; color:#e2e8f0; margin-top:4px; margin-bottom:6px;">${escapeHtml(prosody.overall_fluency_cn || '已分析本次发言。')}</div>
      ${unclearList}
      ${pitchList ? '<div style="margin-top:6px; font-size:0.8rem; color:var(--muted);">重点词东京标准声调（点 🔊 慢速模仿高低走势）：</div>' + pitchList : ''}

      ${errList ? '<h3 style="margin-top:12px;">3. ❌ 逐处错误指出与具体修改意见</h3>' + errList : ''}

      <h3 style="margin-top:12px;">4. ✨ 整句重构版本（建议慢速跟读 2 遍后再按录音键重说本题）</h3>
      <div style="font-size:0.8rem; color:var(--muted); margin-top:4px;">【面谈/技术讨论版（です・ます 口语）】</div>
      <div class="rewrite-jp">
        <span>${escapeHtml(r.polite_colleague_rewrite || '')}</span>
        <div style="display:flex; gap:6px;">
          <button class="small-btn" onclick="speakJapanese('${escapeJs(politeClean)}')">🔊 慢速跟读</button>
          <button class="small-btn secondary" onclick="saveToAccum('${escapeJs(politeClean)}', '面谈/技术讨论版（原话：${escapeJs(r.user_transcript || '')}）', this)">➕ 积累库</button>
        </div>
      </div>

      <div style="font-size:0.8rem; color:var(--muted);">【面试本番版（礼貌的口语，可以直接照着说）】</div>
      <div class="rewrite-jp exec">
        <span>${escapeHtml(r.executive_keigo_rewrite || '')}</span>
        <div style="display:flex; gap:6px;">
          <button class="small-btn" onclick="speakJapanese('${escapeJs(execClean)}')">🔊 慢速跟读</button>
          <button class="small-btn secondary" onclick="saveToAccum('${escapeJs(execClean)}', '面试本番版（原话：${escapeJs(r.user_transcript || '')}）', this)">➕ 积累库</button>
        </div>
      </div>

      <div style="margin:8px 0 12px 0; padding:8px 10px; background:#064e3b33; border:1px solid #059669; border-radius:7px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
        <span style="font-size:0.8rem; color:#a7f3d0;">📬 <b>邮件台账联动：</b>对这轮回答满意后，点右侧按钮即可锁定为今日定稿并解锁明日新课</span>
        <div style="display:flex; gap:6px; flex-wrap:wrap;">
          <button class="small-btn" style="background:#059669; border-color:#10b981; font-weight:700;" onclick="lockDailyToLedger('${cardId}', 'exec', this)">✅ 锁定「面试本番版」写入台账${dayNum ? ' (Day ' + dayNum + ')' : ''}</button>
          <button class="small-btn secondary" onclick="lockDailyToLedger('${cardId}', 'polite', this)">锁定「面谈版」</button>
        </div>
      </div>

      <h3>5. 💡 结构与改进总结</h3>
      <div style="font-size:0.9rem; color:#e2e8f0; margin-top:4px; margin-bottom:8px;">${escapeHtml(r.critique_cn || '')}</div>

      ${vocabHtml ? '<h3>6. 本句核心商务词库</h3>' + vocabHtml : ''}
    </div>
  `;
  reviewScroll.innerHTML = cardHtml + reviewScroll.innerHTML;
}

async function lockDailyToLedger(cardId, variant, btnEl) {
  const r = critiquePayloads[cardId];
  if (!r) return;
  const rewriteJp = (variant === 'polite') ? (r.polite_colleague_rewrite || r.executive_keigo_rewrite) : (r.executive_keigo_rewrite || r.polite_colleague_rewrite);
  const origText = btnEl.textContent;
  btnEl.disabled = true;
  btnEl.textContent = '⏳ 正在写入台账...';
  try {
    const res = await fetch('/api/daily/lock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        rewrite_jp: rewriteJp,
        user_transcript: r.user_transcript || '',
        error_breakdown: r.error_breakdown || [],
        unclear_pronunciations: (r.pronunciation_prosody || {}).unclear_pronunciations || [],
        critique_cn: r.critique_cn || '',
        scores: (r.assessment || {}).scores || {}
      })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      btnEl.textContent = `🎉 已锁定 Day ${data.day} 定稿（明日推 Day ${data.next_day}）`;
      btnEl.style.background = '#047857';
      if (latestPushData) {
        latestPushData.locked = true;
        renderPushBanner();
      }
    } else {
      alert('写入台账失败: ' + (data.error || '未知错误'));
      btnEl.disabled = false;
      btnEl.textContent = origText;
    }
  } catch (e) {
    alert('写入台账请求异常: ' + e.message);
    btnEl.disabled = false;
    btnEl.textContent = origText;
  }
}

async function saveToAccum(jp, cn, btnEl) {
  try {
    const res = await fetch('/api/accumulate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({jp: jp, cn: cn})
    });
    const data = await res.json();
    if (res.ok) {
      btnEl.textContent = '✅ 已存入';
      btnEl.disabled = true;
    } else {
      alert('保存失败: ' + (data.error || ''));
    }
  } catch (e) {
    alert('保存异常: ' + e.message);
  }
}

function toggleReveal(msgId) {
  const jpEl = document.getElementById(msgId + '-jp');
  const hintEl = document.getElementById(msgId + '-hint');
  if (jpEl) jpEl.classList.toggle('blurred');
  if (hintEl) hintEl.classList.toggle('visible');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Tab' && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') {
    e.preventDefault();
    const blurredEls = document.querySelectorAll('.jp-text');
    if (blurredEls.length > 0) {
      const last = blurredEls[blurredEls.length - 1];
      const msgId = last.id.replace('-jp', '');
      toggleReveal(msgId);
    }
  }
  if (e.key === 'Enter' && e.ctrlKey) {
    sendText();
  }
});

function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escapeJs(str) {
  return String(str || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, ' ').replace(/\r/g, '');
}

initConfig();
</script>
</body>
</html>
"""


# ============================================================
# 面试题库：模板生成 + 三阶段练习（跟读 → 填空 → 自由）
# ============================================================
BANK_JSON = SPEAKING_DIR / "面试题库.json"
BANK_STAGE_DIR = SESSION_DIR / "bank_stage"  # 手动调整阶段的事件，按「日期_电脑名」分文件
STAGES = {1: "跟读", 2: "填空", 3: "自由", 4: "已掌握"}


def _spoken_rules() -> str:
    s = SYSTEM_PROMPT_TEMPLATE
    a = s.find("【最重要：口語スタイル原則")
    b = s.find("【あなたのタスク】")
    return s[a:b].strip() if a >= 0 and b > a else ""


def load_bank() -> dict:
    if not BANK_JSON.exists():
        return {"questions": []}
    return json.loads(BANK_JSON.read_text(encoding="utf-8"))


def save_bank(data: dict) -> None:
    tmp = BANK_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, BANK_JSON)
    _trigger_bg_sync()


def _find_question(data: dict, qid: str) -> dict:
    for q in data.get("questions", []):
        if q.get("id") == qid:
            return q
    raise KeyError(f"题库里没有 id={qid} 的题")


def strip_furigana(text: str) -> str:
    import re
    return re.sub(r"[（(][ぁ-ゖァ-ヺー・\s]+[）)]", "", text or "")


TEMPLATE_PROMPT = """あなたは日本企業の面接対策を専門とするコーチです。〔CANDIDATE〕のために、面接の質問に対する「回答テンプレート」を作ります。

【候補者の事実情報（ここに書かれた事実だけを使うこと）】
{profile}

【絶対ルール：事実を作らない】
- 上記の事実情報にない数字・エピソード・企業名・経験は絶対に作らないでください。
- 必要なのに情報がない部分は、台本の中に【要補足：〇〇（例：解析時間が何割短くなったか）】と書いて空けてください。
- その空欄は fill_in_slots_cn にも中国語で列挙してください。

{spoken_rules}

【質問】
{question_jp}
（面接官の意図：{intent_cn}）
（目標の長さ：約{target_seconds}秒。話す速さは1秒あたり約5〜6文字なので、台本は約{target_chars}文字）

次のJSONスキーマで出力してください（純粋なJSONのみ）：
{{
  "skeleton": [
    {{"part": "P 結論 / R 理由 / E 具体例 / P 締め など、パート名", "jp": "誰でも使える汎用の骨組み。入れ替える部分は【】で示す。話し言葉で。", "cn": "このパートで言うべきことの中国語説明"}}
  ],
  "script_jp": "この候補者用の完成台本。難読漢字にはカッコでひらがなを付ける。【要補足：…】はそのまま残す。",
  "script_clean": "script_jp からふりがなのカッコを除いた読み上げ用テキスト",
  "script_cn": "台本の中国語訳",
  "segments": [
    {{"part": "skeleton と同じパート名", "jp_clean": "台本のうちこのパートに当たる部分（ふりがななし）"}}
  ],
  "cloze_keywords": ["script_clean にそのまま出てくる、覚えるべきキーフレーズを6〜10個（2〜12文字程度、助詞は含めない）"],
  "fill_in_slots_cn": ["素材に足りず、本人が補うべき情報（中国語）"],
  "tips_cn": ["この質問に答えるときの注意点を1〜3個（中国語）"]
}}
skeleton は3〜4パート、segments は skeleton と同じ順番・同じパート名にしてください。
"""


def _short_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def profile_hash() -> str:
    """个人素材的版本号：模板记下生成时的版本，素材一改就能看出哪些模板过时"""
    return _short_hash(load_personal_profile())


MASTER_FACTS = PROJECT_ROOT / "01_自分の材料" / "自己分析_PR" / "Master_Facts.md"


def profile_info() -> dict:
    pm = PROFILE_MD.stat().st_mtime if PROFILE_MD.exists() else 0
    mm = MASTER_FACTS.stat().st_mtime if MASTER_FACTS.exists() else 0
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
    return {
        "hash": profile_hash(),
        "profile_mtime": fmt(pm),
        "master_mtime": fmt(mm),
        # Master_Facts 是正本；它比 个人素材 新超过 1 分钟，说明还没同步过来
        "master_newer": bool(mm and mm - pm > 60),
    }


def _build_template(q: dict, extra_block: str = "") -> dict:
    cfg = load_config()
    secs = int(q.get("target_seconds") or 45)
    system = TEMPLATE_PROMPT.format(
        profile=load_personal_profile() or "（プロフィール未読込）",
        spoken_rules=_spoken_rules(),
        question_jp=q.get("question_jp", ""),
        intent_cn=q.get("intent_cn", ""),
        target_seconds=secs,
        target_chars=secs * 5,
    )
    if extra_block:
        system = system.replace("【質問】", extra_block.strip() + "\n\n【質問】", 1)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": "テンプレートを作成してください。"}]}],
        "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"},
    }
    tpl = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    tpl.pop("_endpoint", None)
    if not tpl.get("script_clean"):
        tpl["script_clean"] = strip_furigana(tpl.get("script_jp", ""))
    tpl["cloze_keywords"] = [k for k in tpl.get("cloze_keywords", []) if k and k in tpl["script_clean"]]
    tpl["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tpl["profile_hash"] = profile_hash()
    tpl["edited"] = False
    return tpl


def _template_gemini_allowed() -> None:
    """模板（通用版 / 公司版）改由 Claude 在对话里写（用户 2026-11-14）；config 里 TEMPLATE_ALLOW_GEMINI=1 才恢复网页生成"""
    if str(load_config().get("TEMPLATE_ALLOW_GEMINI", "")).lower() not in ("1", "true", "yes"):
        raise ValueError("模板改由 Claude 在对话里写，网页不再调 Gemini 生成。请在和 Claude 的对话里说「写模板：题目或公司名」。")


def generate_template(qid: str) -> dict:
    _template_gemini_allowed()
    tpl = _build_template(_find_question(load_bank(), qid))
    # 重新读一次再写，避免覆盖期间别的题的改动
    data = load_bank()
    _find_question(data, qid)["template"] = tpl
    save_bank(data)
    return tpl


def save_template_edit(qid: str, script_jp: str) -> dict:
    data = load_bank()
    q = _find_question(data, qid)
    tpl = q.get("template") or {}
    tpl["script_jp"] = script_jp.strip()
    tpl["script_clean"] = strip_furigana(tpl["script_jp"])
    tpl["cloze_keywords"] = [k for k in tpl.get("cloze_keywords", []) if k in tpl["script_clean"]]
    tpl["edited"] = True
    tpl["edited_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q["template"] = tpl
    save_bank(data)
    return tpl


STAGE_INSTRUCTIONS = {
    1: "【段階1：跟読（音読）】ユーザーは模範台本を見ながら音読しています。内容の評価はせず、読み間違い・発音（長音/促音/清濁）・アクセント・リズム・つかえを診断してください。logic は台本通りの順で読めたかで判断。台本をほぼ正確に、大きなつかえなく読めていれば ready_for_next_stage=true。",
    2: "【段階2：穴埋め】ユーザーは台本のキーフレーズが隠された状態で回答しています。各パートを言えたか（point_check）と、キーフレーズをどれだけ思い出せたかを中心に評価してください。言い回しが台本と少し違っても、自然で意味が同じなら正解扱い。全パートを言えて、キーフレーズの大半を思い出せていれば ready_for_next_stage=true。",
    3: "【段階3：自由回答】ユーザーは台本を見ずに自分の言葉で答えています。台本と同じ言い回しは求めません。各パートの論理を言えたか、話し言葉として自然か、誤りがないかを評価してください。own_words_version_jp は「ユーザー自身の言い方と内容を残したまま」直した版にし、台本の文に戻さないでください。全パートを言えて重大な誤りがなければ ready_for_next_stage=true（＝この質問は習得）。",
}

PRACTICE_PROMPT = """あなたは面接日本語の話し方コーチです。ユーザー（中国語母語話者・JLPT N1）が、ある面接質問を段階的に練習しています。

{spoken_rules}

【質問】{question_jp}
【このユーザー用の模範台本】
{script_clean}
【台本のパート構成】
{segments}
【キーフレーズ】{keywords}

{stage_instruction}

次のJSONスキーマで出力してください（純粋なJSONのみ）：
{{
  "user_transcript": "ユーザーの発言の忠実な書き起こし（言い淀みも含める）",
  "point_check": [{{"part": "パート名（台本のパート構成と同じ）", "covered": true, "note_cn": "抜けや弱さがあれば中国語で一言"}}],
  "keywords_recalled": ["言えたキーフレーズ"],
  "scores": {{"grammar": "1〜10", "keigo": "1〜10", "logic": "1〜10", "fluency": "1〜10", "pronunciation": "音声なら1〜10、文字入力なら null"}},
  "problem_tags": ["見られた問題のみ、次のIDから最大4つ: {problem_tag_ids}"],
  "error_breakdown": [{{"original_part": "❌ 問題のある部分", "fixed_part": "✅ 直した言い方（話し言葉）", "reason_cn": "💡 中国語で理由"}}],
  "pronunciation_issues": [{{"heard_as": "聞こえた音", "target_word": "正しい語（かな付き）", "tip_cn": "中国語でコツ"}}],
  "own_words_version_jp": "ユーザー自身の内容と言い方を残して、誤りと不自然な所だけ直した版（ふりがな付き）",
  "own_words_version_clean": "上のふりがななし版",
  "one_line_progress_cn": "一句话中文点评",
  "ready_for_next_stage": true,
  "next_stage_advice_cn": "下一遍最该注意的 1〜2 点（中文）"
}}
"""


def practice_bank(qid: str, stage: int, user_text: str = "", audio_b64: str = "", audio_mime: str = "audio/webm", company: str = "", ctx_question: str = "", ctx_company: str = "") -> dict:
    cfg = load_config()
    data = load_bank()
    q = _find_question(data, qid)
    tpl = q.get("template") or {}
    if company:
        tpl = load_company_bank().get("companies", {}).get(company, {}).get("templates", {}).get(qid) or tpl
    if not tpl.get("script_clean"):
        raise ValueError("这道题还没有模板，请先点「生成模板」。")
    stage = stage if stage in (1, 2, 3) else 1
    segs = "\n".join(f"- {s.get('part', '')}: {s.get('jp_clean', '')}" for s in tpl.get("segments", [])) or "（なし）"
    system = PRACTICE_PROMPT.format(
        spoken_rules=_spoken_rules(),
        question_jp=(ctx_question.strip() + f"\n（この質問は{ctx_company or 'ある企業'}の背景を踏まえた言い換え版。基本の質問は「{q.get('question_jp', '')}」。回答が背景に噛み合っているかも logic の評価に含めること。模範台本は汎用版なので、台本と違う切り口でも背景に合っていれば良しとする）") if ctx_question.strip() else q.get("question_jp", ""),
        script_clean=tpl.get("script_clean", ""),
        segments=segs,
        keywords="、".join(tpl.get("cloze_keywords", [])) or "（なし）",
        stage_instruction=STAGE_INSTRUCTIONS[stage],
        problem_tag_ids=", ".join(f"{k}({v})" for k, v in PROBLEM_TAGS.items()),
    )
    parts = []
    if audio_b64:
        parts.append({"inlineData": {"mimeType": audio_mime, "data": audio_b64}})
        parts.append({"text": "添付の音声が私の回答です。書き起こして評価してください。" + (f"\n補足: {user_text}" if user_text.strip() else "")})
    else:
        if not user_text.strip():
            raise ValueError("没有收到回答（录音或文字）。")
        parts.append({"text": user_text.strip()})
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    r = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    # 填空阶段的目的就是记住模板：关键词至少记住 60% 才算通过
    kws = tpl.get("cloze_keywords", [])
    if stage == 2 and kws:
        got = [k for k in kws if k in set(r.get("keywords_recalled") or []) or k in (r.get("user_transcript") or "")]
        r["keywords_recalled"] = got
        if len(got) < 0.6 * len(kws) and r.get("ready_for_next_stage"):
            r["ready_for_next_stage"] = False
            r["next_stage_advice_cn"] = (f"关键词只记住了 {len(got)}/{len(kws)} 个（需要 60% 以上才过这一关），再看一遍模板记一下没说出来的词。"
                                         + (r.get("next_stage_advice_cn") or ""))
    tags = [t for t in (r.get("problem_tags") or []) if t in PROBLEM_TAGS]
    append_session_record({
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine": MACHINE,
        "scenario": "bank",
        "question_id": qid,
        "company": company,
        "stage": stage,
        "passed": bool(r.get("ready_for_next_stage")),
        "model": cfg.get("GEMINI_MODEL", ""),
        "question": ctx_question.strip() or q.get("question_jp", ""),
        "ctx_company": ctx_company,
        "input_type": "audio" if audio_b64 else "text",
        "user_transcript": r.get("user_transcript", ""),
        "scores": _clean_scores(r.get("scores")),
        "problem_tags": tags,
        "one_line_progress_cn": r.get("one_line_progress_cn", ""),
        "error_breakdown": r.get("error_breakdown", []),
        "unclear_pronunciations": r.get("pronunciation_issues", []),
        "point_check": r.get("point_check", []),
        "own_words_version": r.get("own_words_version_jp", ""),
        "critique_cn": r.get("next_stage_advice_cn", ""),
    })
    r["stage_status"] = bank_status().get(qid, {})
    return r


def set_bank_stage(qid: str, stage: int) -> None:
    BANK_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (BANK_STAGE_DIR / f"{now[:10]}_{MACHINE}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": now, "question_id": qid, "stage": int(stage), "machine": MACHINE}, ensure_ascii=False) + "\n")
    _trigger_bg_sync()


def bank_status() -> dict:
    """按时间回放：手动设定 → 直接到该阶段；某阶段通过 → 进入下一阶段（最多 4=已掌握）"""
    events = []
    for r in load_all_records():
        if r.get("scenario") == "bank" and r.get("question_id"):
            events.append(("practice", r))
    if BANK_STAGE_DIR.exists():
        for fp in BANK_STAGE_DIR.glob("*.jsonl"):
            for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    events.append(("set", json.loads(line)))
                except json.JSONDecodeError:
                    pass
    events.sort(key=lambda e: e[1].get("timestamp", ""))
    st = {}
    for kind, e in events:
        s = st.setdefault(e["question_id"], {"stage": 1, "attempts": 0, "last_ts": "", "last_avg": None})
        if kind == "set":
            s["stage"] = max(1, min(4, int(e.get("stage", 1))))
            continue
        s["attempts"] += 1
        s["last_ts"] = e.get("timestamp", "")
        vals = list((e.get("scores") or {}).values())
        s["last_avg"] = round(sum(vals) / len(vals), 1) if vals else None
        stg = int(e.get("stage", 1))
        if e.get("passed") and stg >= s["stage"]:
            s["stage"] = min(4, stg + 1)
    return st


# ---------------------------------------------------------------------------
# 公司版模板：通用模板不动；和具体公司有关的几道题，另存一份某公司专用的版本
# ---------------------------------------------------------------------------
COMPANY_DIR = PROJECT_ROOT / "02_企業別"
COMPANY_BANK_JSON = SPEAKING_DIR / "面试题库_公司版.json"
DEFAULT_COMPANY_QIDS = ["jikoshokai", "research_application", "why_company", "future_plan", "reverse_question"]
_MATERIAL_SKIP = ("CV_", "履歴書", "cover_letter", "中文对照")
_MATERIAL_PER_FILE = 8000
_MATERIAL_TOTAL = 30000


def company_qids(data: dict = None) -> list:
    data = data or load_bank()
    marked = [q["id"] for q in data.get("questions", []) if q.get("company_specific")]
    return marked or [q for q in DEFAULT_COMPANY_QIDS if any(x.get("id") == q for x in data.get("questions", []))]


def list_company_folders() -> list:
    if not COMPANY_DIR.exists():
        return []
    out = [d.name for d in COMPANY_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    return sorted(out, key=lambda n: (n.endswith("_miss"), n.lower()))


def _redact(text: str) -> str:
    """公司资料里可能混着 ES 草稿：发给 Gemini 前去掉邮箱、电话、邮编"""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "（メール省略）", text)
    text = re.sub(r"(?<!\d)0\d{1,3}[-‐ー−]?\d{2,4}[-‐ー−]?\d{3,4}(?!\d)", "（電話省略）", text)
    text = re.sub(r"〒\s?\d{3}-?\d{4}[^\n]*", "（住所省略）", text)
    return text


def read_company_materials(folder: str) -> dict:
    """读 02_企業別/<folder>/ 下的 .md/.txt（跳过简历、cover letter），按'调研/面试准备'优先"""
    if not folder:
        return {"text": "", "files": []}
    base = (COMPANY_DIR / folder).resolve()
    if COMPANY_DIR.resolve() not in base.parents or not base.is_dir():
        return {"text": "", "files": []}
    files = [p for p in base.rglob("*") if p.suffix.lower() in (".md", ".txt") and not any(s in p.name for s in _MATERIAL_SKIP)]
    pri = lambda p: (0 if re.search(r"research|analysis|notes|研究|企業", p.name, re.I) else
                     1 if re.search(r"prep|interview|面談|面接|plan", p.name, re.I) else 2, p.name)
    chunks, used, total = [], [], 0
    for p in sorted(files, key=pri):
        if total >= _MATERIAL_TOTAL:
            break
        t = _redact(p.read_text(encoding="utf-8", errors="ignore").strip())[:_MATERIAL_PER_FILE]
        t = t[: _MATERIAL_TOTAL - total]
        if not t:
            continue
        chunks.append(f"### {p.relative_to(base).as_posix()}\n{t}")
        used.append(p.relative_to(base).as_posix())
        total += len(t)
    return {"text": "\n\n".join(chunks), "files": used}


def load_company_bank() -> dict:
    if not COMPANY_BANK_JSON.exists():
        return {"companies": {}}
    return json.loads(COMPANY_BANK_JSON.read_text(encoding="utf-8"))


def save_company_bank(data: dict) -> None:
    tmp = COMPANY_BANK_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COMPANY_BANK_JSON)
    _trigger_bg_sync()


def _company_key(name: str) -> str:
    key = re.sub(r"[\\/:*?\"<>|\s]+", "_", (name or "").strip()).strip("_")
    if not key:
        raise ValueError("公司名不能为空。")
    return key


def upsert_company(name: str, folder: str = "", memo: str = "") -> dict:
    data = load_company_bank()
    key = _company_key(name)
    c = data["companies"].setdefault(key, {"name": name.strip(), "templates": {}, "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    c["name"] = name.strip()
    if folder and folder not in list_company_folders():
        raise ValueError(f"02_企業別 下没有文件夹 {folder}")
    c["folder"] = folder
    c["memo"] = memo.strip()
    save_company_bank(data)
    return {"key": key, **c}


def delete_company(key: str) -> None:
    data = load_company_bank()
    data["companies"].pop(key, None)
    save_company_bank(data)


def _company_materials_hash(c: dict) -> str:
    return _short_hash(read_company_materials(c.get("folder", ""))["text"] + "\n" + (c.get("memo") or ""))


COMPANY_BLOCK = """【応募先企業：{name}】
この回答は「{name}」の面接用です。次の企業情報（本人がまとめた調査メモ・面接準備資料）を使って、会社固有の部分を具体的にしてください。
- 企業についての事実（事業名・製品・技術・数字・社風など）は、下の企業情報に書かれていることだけを使うこと。書かれていないことは推測で書かず【要補足：〇〇】で空ける。
- 企業資料は「企業について」の情報源としてだけ使うこと。企業資料の中にある候補者についての記述（自己PR・志望動機の下書き・「接点」「強み」の分析など）は古い・誇張されている可能性があるので、一切使わない。候補者の経験・強み・数字・表現は、必ず上の【候補者の事実情報】から取る（例：事実情報が「既存ツールを導入した」なら「ゼロから立ち上げた」「開発した」とは書かない）。
- 汎用版の台本がある場合は、構成と言い回しをできるだけ残し、会社に合わせて変える部分だけ変える（本人が汎用版を覚えているため）。

〈本人のメモ〉
{memo}

〈企業資料（02_企業別/{folder}/）〉
{materials}

〈汎用版の台本〉
{base_script}
"""


def generate_company_template(key: str, qid: str) -> dict:
    _template_gemini_allowed()
    cdata = load_company_bank()
    c = cdata["companies"].get(key)
    if not c:
        raise KeyError(f"还没有建立公司版：{key}")
    q = _find_question(load_bank(), qid)
    mats = read_company_materials(c.get("folder", ""))
    if not mats["text"] and not (c.get("memo") or "").strip():
        raise ValueError("这家公司既没有资料文件夹也没有备注，Gemini 没有依据可写。请先选资料文件夹或写几句公司备注。")
    block = COMPANY_BLOCK.format(
        name=c["name"],
        memo=(c.get("memo") or "").strip() or "（なし）",
        folder=c.get("folder") or "（なし）",
        materials=mats["text"] or "（なし）",
        base_script=((q.get("template") or {}).get("script_clean")) or "（なし）",
    )
    tpl = _build_template(q, block)
    tpl["materials_hash"] = _company_materials_hash(c)
    tpl["material_files"] = mats["files"]
    cdata = load_company_bank()
    cdata["companies"][key].setdefault("templates", {})[qid] = tpl
    save_company_bank(cdata)
    return tpl


def save_company_template_edit(key: str, qid: str, script_jp: str) -> dict:
    cdata = load_company_bank()
    tpl = cdata["companies"][key]["templates"][qid]
    tpl["script_jp"] = script_jp.strip()
    tpl["script_clean"] = strip_furigana(tpl["script_jp"])
    tpl["cloze_keywords"] = [k for k in tpl.get("cloze_keywords", []) if k in tpl["script_clean"]]
    tpl["edited"] = True
    tpl["edited_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_company_bank(cdata)
    return tpl


def companies_summary() -> dict:
    """给页面用：每家公司的模板 + 资料是否变过（materials_changed）"""
    out = {}
    for key, c in load_company_bank().get("companies", {}).items():
        mh = _company_materials_hash(c)
        tpls = c.get("templates", {})
        for t in tpls.values():
            t["materials_changed"] = bool(t.get("materials_hash")) and t["materials_hash"] != mh
        out[key] = {**c, "templates": tpls}
    return out


# ---------------------------------------------------------------------------
# 背景题：同一道通用题，随机套上某家公司 / JD 的背景来问（只存问法，不动模板）
# ---------------------------------------------------------------------------
CTXQ_JSON = SPEAKING_DIR / "面试题库_背景题.json"

CTXQ_PROMPT = """あなたは日本企業の面接官役です。〔CANDIDATE〕である候補者の練習用に、下の「基本の質問」を、指定の企業・募集職種の背景を踏まえた形に言い換えます。

【企業】{name}
【その企業の資料（募集要項・企業研究・面接準備メモなど。ここに書かれていることだけを背景に使うこと）】
{materials}

【基本の質問】{question_jp}
（質問の本来の意図：{intent_cn}）

【ルール】
- 質問の本来の意図は変えない。面接官が実際に話す口調で、1〜3文。冒頭か途中で、資料にある「この企業・この職種が重視している点」（事業内容・求める人物像・業務内容・技術テーマ等）を1つだけ背景として添える。
- 資料にない事実・数字・制度を作らない。資料から具体的な背景が読み取れなければ background_ok を false にして、question_jp は基本の質問のままにする。
- 候補者の経歴・実績について、面接官が知らないはずのことを前提にしない（「あなたの○○研究では」と決めつけない）。
- 毎回同じ切り口にならないよう、資料の中から{angle_hint}の観点を優先して選ぶ。

次のJSONのみで出力：
{{
  "background_ok": true,
  "question_jp": "背景つきの質問（面接官の話し言葉）",
  "question_cn": "中国語訳",
  "background_cn": "この質問の背景に使った資料上の事実（中国語で1〜2文、どの点か分かるように）",
  "focus_cn": "この企業の関心を踏まえると、通用の答えに比べて何を強調／言い換えるべきか（中国語で1〜2文）"
}}
"""

CTXQ_ANGLES = ["事業・製品", "募集職種の業務内容", "求める人物像・社風", "技術・研究テーマ", "キャリア・成長環境", "課題・今後の方向性"]


def load_ctxq() -> dict:
    if not CTXQ_JSON.exists():
        return {"items": []}
    try:
        return json.loads(CTXQ_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def save_ctxq(data: dict) -> None:
    tmp = CTXQ_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CTXQ_JSON)
    _trigger_bg_sync()


def generate_context_question(qid: str, folder: str = "") -> dict:
    """给 qid 这道题加上公司背景。folder 为空 = 在 02_企業別 里随机抽一家有资料的公司（_archive 不抽）。"""
    import random
    q = _find_question(load_bank(), qid)
    lang = load_ctxq().get("company_lang", {})
    cands = [f for f in list_company_folders() if not f.startswith("_") and lang.get(f, "ja") == "ja"]
    if folder:
        cands = [folder]
    random.shuffle(cands)
    mats = name = None
    for f in cands[:12]:
        m = read_company_materials(f)
        if len(m["text"]) >= 300:
            mats, name, folder = m, f, f
            break
    if not mats:
        raise ValueError("02_企業別 里没有找到带资料的公司文件夹（需要 .md/.txt，且不是简历/ES）。")
    system = CTXQ_PROMPT.format(
        name=name.replace("_miss", ""), materials=mats["text"],
        question_jp=q.get("question_jp", ""), intent_cn=q.get("intent_cn", ""),
        angle_hint=random.choice(CTXQ_ANGLES),
    )
    cfg = load_config()
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": "背景つきの質問を作ってください。"}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }
    r = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    r.pop("_endpoint", None)
    if not r.get("question_jp"):
        raise ValueError("Gemini 没有返回问题，请再点一次。")
    item = {
        "qid": qid, "company": folder, "question_jp": r["question_jp"].strip(), "question_cn": r.get("question_cn", ""),
        "background_cn": r.get("background_cn", ""), "focus_cn": r.get("focus_cn", ""),
        "background_ok": bool(r.get("background_ok", True)), "material_files": mats["files"],
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data = load_ctxq()
    data.setdefault("items", []).append(item)
    data["items"] = data["items"][-300:]
    save_ctxq(data)
    return item


def pick_context_question(qid: str, folder: str = "") -> dict:
    """背景题：优先从题池（Claude 在对话里根据 02_企業別 提前写好的）里取，挑用得最少的；
    题池没有时默认不调 Gemini（config 里 CTXQ_ALLOW_GEMINI=1 才现场生成）。只出日语公司的题。"""
    import random
    data = load_ctxq()
    lang = data.get("company_lang", {})
    pool = [it for it in data.get("pool", []) if it.get("qid") == qid and lang.get(it.get("company"), "ja") == "ja"]
    if folder:
        pool = [it for it in pool if it.get("company") == folder]
    if pool:
        used = data.setdefault("used", {})
        least = min(used.get(it["id"], 0) for it in pool)
        it = random.choice([x for x in pool if used.get(x["id"], 0) == least])
        used[it["id"]] = used.get(it["id"], 0) + 1
        save_ctxq(data)
        return {**it, "source": "pool", "pool_size": len(pool), "times_used": used[it["id"]]}
    if str(load_config().get("CTXQ_ALLOW_GEMINI", "")).lower() in ("1", "true", "yes"):
        return {**generate_context_question(qid, folder), "source": "gemini"}
    if folder and lang.get(folder) == "en":
        raise ValueError("这家公司归为英语面试，日语题库不出它的背景题。")
    raise ValueError("题池里还没有" + ("这家公司的" if folder else "") + "这道题的背景版。在和 Claude 的对话里说「补背景题」，会根据 02_企業別 的资料提前写好（不调 Gemini）。")


def ctxq_counts() -> dict:
    data = load_ctxq()
    lang = data.get("company_lang", {})
    out = {}
    for it in data.get("pool", []):
        if lang.get(it.get("company"), "ja") == "ja":
            out[it["qid"]] = out.get(it["qid"], 0) + 1
    return out


def bank_payload() -> dict:
    data = load_bank()
    return {
        "questions": data.get("questions", []),
        "status": bank_status(),
        "stages": STAGES,
        "tags": PROBLEM_TAGS,
        "profile": profile_info(),
        "company_qids": company_qids(data),
        "companies": companies_summary(),
        "company_folders": list_company_folders(),
        "ctxq_counts": ctxq_counts(),
    }


# ---------------------------------------------------------------------------
# 生活场景：卡片内容事先写好（06_日本語口語/生活场景/*.json），Gemini 只在角色扮演时实时对话和批改
# ---------------------------------------------------------------------------
SCENE_DIR = SPEAKING_DIR / "生活场景"
SCENE_ARCHIVE_DIR = SCENE_DIR / "archive"
SCENE_STATE_FILE = SCENE_DIR / "_scene_state.json"


def load_scene_state() -> dict:
    if SCENE_STATE_FILE.exists():
        try:
            data = json.loads(SCENE_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("scenes", {})
                return data
        except Exception as e:
            log_error("load_scene_state", str(e))
    return {"scenes": {}}


def save_scene_state(state: dict) -> None:
    SCENE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCENE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SCENE_STATE_FILE)
    _trigger_bg_sync()


def update_scene_status(sid: str, status: str, note: str = "") -> dict:
    """更新场景状态：active（在练） / marked_archive（已练完·待归档） / archived（已归档）"""
    if status not in ("active", "marked_archive", "archived"):
        raise ValueError(f"无效状态: {status}")
    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    SCENE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_scene_state()
    entry = state["scenes"].setdefault(sid, {})
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry["status"] = status
    entry["updated_at"] = now_str
    if status == "marked_archive":
        entry["marked_at"] = now_str
    elif status == "archived":
        entry["archived_at"] = now_str
    if note:
        entry["note"] = note.strip()

    active_fp = SCENE_DIR / f"{sid}.json"
    arch_fp = SCENE_ARCHIVE_DIR / f"{sid}.json"
    if status == "archived" and active_fp.exists():
        os.replace(active_fp, arch_fp)
    elif status in ("active", "marked_archive") and not active_fp.exists() and arch_fp.exists():
        os.replace(arch_fp, active_fp)

    save_scene_state(state)
    return {"id": sid, **entry}


def load_scenes() -> list:
    out = []
    state = load_scene_state().get("scenes", {})
    seen_ids = set()
    if SCENE_DIR.exists():
        for fp in sorted(SCENE_DIR.glob("*.json")):
            if fp.name.startswith("_"):
                continue
            try:
                sc = json.loads(fp.read_text(encoding="utf-8"))
                sid = sc.get("id") or fp.stem
                meta = state.get(sid, {})
                st = meta.get("status", "active")
                if st == "archived":
                    st = "active"
                sc["status"] = st
                sc["marked_at"] = meta.get("marked_at", "")
                sc["archived_at"] = meta.get("archived_at", "")
                sc["user_note"] = meta.get("note", "")
                out.append(sc)
                seen_ids.add(sid)
            except json.JSONDecodeError as e:
                log_error(f"scene {fp.name}", str(e))
    if SCENE_ARCHIVE_DIR.exists():
        for fp in sorted(SCENE_ARCHIVE_DIR.glob("*.json")):
            if fp.name.startswith("_"):
                continue
            try:
                sc = json.loads(fp.read_text(encoding="utf-8"))
                sid = sc.get("id") or fp.stem
                if sid in seen_ids:
                    continue
                meta = state.get(sid, {})
                sc["status"] = "archived"
                sc["marked_at"] = meta.get("marked_at", "")
                sc["archived_at"] = meta.get("archived_at", "")
                sc["user_note"] = meta.get("note", "")
                out.append(sc)
                seen_ids.add(sid)
            except json.JSONDecodeError as e:
                log_error(f"archive scene {fp.name}", str(e))
    return out


def _find_scene(sid: str) -> dict:
    for s in load_scenes():
        if s.get("id") == sid:
            return s
    raise KeyError(f"没有这个场景：{sid}")


def _scene_context(scene: dict, part: str, curveballs: list) -> str:
    p = next((x for x in scene.get("parts", []) if x.get("id") == part), None) or scene["parts"][0]
    rp = next((x for x in scene.get("roleplay", []) if x.get("part") == part), {})
    lines = "\n".join(f"- {'あなた（相手役）' if l['who'] != 'me' else 'ユーザー'}: {l['jp']}" for l in p.get("lines", []))
    cbs = "\n".join(f"- {c['situation_cn']}：例「{c['npc_jp']}」" for c in curveballs) or "（なし）"
    return f"""【場面】{scene.get('title')}／{rp.get('title', p.get('title'))}
【状況】{scene.get('setting_cn')}
【あなたの役】{rp.get('npc_role', '相手役')}
【ユーザーの目標】{rp.get('goal_cn', '')}
【参考の流れ（この通りでなくてよい。ユーザーの発言に合わせて自然に進める）】
{lines}
【今回途中で1回ずつ自然に入れるハプニング（まだ入れていなければ、流れに合うタイミングで入れる）】
{cbs}"""


SCENE_TURN_PROMPT = """あなたは日本語会話のロールプレイ相手兼コーチです。ユーザーは〔CANDIDATE_LIFE〕で、日本の日常生活の場面を練習しています。

{context}

【相手役としてのルール】
- 実際の日本のスタッフ・医師が話すような、自然でリアルな話し方（丁寧語中心、普通の速さの言い回し、「〜ですね」「〜てもらえますか」など）。教科書的に言い換えない。
- 1回の発話は短く（1〜3文）。一度に複数の質問をしない。
- **重要：`npc.jp` には相手が実際に声に出すセリフだけを書くこと。「（点検後）」「（ピンポーン）」「（笑顔で）」のようなト書き・状況説明は絶対に書かないでください（TTSがそのまま読み上げてしまうため）。**
- ユーザーの返答に合わせて会話を自然に進める。ユーザーの日本語が不自然でも、意味が通じれば会話は続ける（通じなければ現実のスタッフのように聞き返す）。
- 場面の目標が達成されたら、締めの一言を言って scene_done=true にする。

【コーチとしてのルール】
- ユーザーの今回の発言だけを短く評価する。現実の場面で問題なく通じる・自然なら ok=true、fixes は空でよい。
- 直すのは「通じない／失礼／不自然で浮く」所だけ。細かすぎる指摘はしない。fixes は最大2つ。
- better は、その場で実際に言う自然な話し言葉にする（書き言葉・硬すぎる敬語にしない）。

次のJSONで出力（純粋なJSONのみ）：
{{
  "user_transcript": "ユーザーの今回の発言の書き起こし（音声の場合。文字入力ならそのまま）",
  "feedback": {{"ok": true, "fixes": [{{"original": "問題の部分", "better": "自然な言い方", "reason_cn": "中文理由（一句）"}}], "tip_cn": "（任意）这一句可以更好的一点，中文，一句话以内；没有就空"}},
  "npc": {{"jp": "相手役の次の発話", "cn": "中文翻译"}},
  "curveball_used": "今回ハプニングを入れたなら、その situation の中国語。入れていなければ空",
  "scene_done": false
}}
"""

SCENE_FINISH_PROMPT = """あなたは日本語会話のコーチです。中国語母語話者（JLPT N1）が、日常生活の場面のロールプレイを終えました。会話全体を見て総評してください。

{context}

【会話の記録】
{transcript}

【会話中にすでに出した指摘】
{fixes}

【総評のルール】
- 基準は「実際の窓口・電話で自然に通じるか」。日常会話なので、「〜んですが」「〜って何ですか」「〜てもらえますか」のような自然な話し言葉を正解とする。
- review_phrases も実際にその場で言う話し言葉で書く。「〜のですが」「〜でしょうか」「〜いただけますでしょうか」のような硬い形に言い換えない。
- 会話中の指摘と矛盾させない（会話中に問題なしとした表現を、ここで直さない）。
- logic は「場面の目標をどこまで達成できたか」。ユーザーが途中で終えた場合は、そこまでのやりとりの質も考慮する。

次のJSONで出力（純粋なJSONのみ）：
{{
  "scores": {{"grammar": 1〜10, "keigo": 1〜10, "logic": "目標を達成できたか 1〜10", "fluency": 1〜10, "pronunciation": "音声の発言があれば 1〜10、なければ null"}},
  "goal_achieved": true,
  "problem_tags": ["見られた問題のみ、次のIDから最大4つ: {problem_tag_ids}"],
  "good_points_cn": ["做得好的地方，1〜2 条"],
  "review_phrases": [{{"jp": "この場面で覚えておくと良い自然な言い方（ユーザーが言えなかった・不自然だったものを優先）", "cn": "中文"}}],
  "one_line_progress_cn": "一句话中文总评"
}}
"""


def _scene_audio_parts(user_text: str, audio_b64: str, audio_mime: str) -> list:
    if audio_b64:
        return [{"inlineData": {"mimeType": audio_mime, "data": audio_b64}},
                {"text": "添付の音声が私の今回の発言です。" + (f"\n補足: {user_text}" if user_text.strip() else "")}]
    if not user_text.strip():
        raise ValueError("没有收到你的回答（录音或文字）。")
    return [{"text": "私の今回の発言: " + user_text.strip()}]


def _history_text(history: list) -> str:
    return "\n".join(f"{'相手' if h.get('role') == 'npc' else 'ユーザー'}: {h.get('jp', '')}" for h in history or []) or "（まだなし）"


def scene_turn(sid: str, part: str, curveballs: list, history: list, user_text: str = "", audio_b64: str = "", audio_mime: str = "audio/webm") -> dict:
    cfg = load_config()
    scene = _find_scene(sid)
    system = SCENE_TURN_PROMPT.format(context=_scene_context(scene, part, curveballs))
    parts = [{"text": "【ここまでの会話】\n" + _history_text(history)}] + _scene_audio_parts(user_text, audio_b64, audio_mime)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.6, "responseMimeType": "application/json"},
    }
    r = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    r.pop("_endpoint", None)
    if not audio_b64 and not r.get("user_transcript"):
        r["user_transcript"] = user_text.strip()
    return r


SCENE_PHRASE_CHECK_PROMPT = """あなたは日本語の日常会話・発音コーチです。ユーザー（中国語母語話者・JLPT N1）が「{scene_title}」の場面で、中国語の提示を見て自分で日本語を声に出して（または入力して）言ってみました。

【言いたい内容（中国語）】{target_cn}
【参考の自然な表現】{target_jp}

【判定ルール】
- 参考表現と一字一句同じである必要はまったくありません。実際の日本の窓口・電話・店舗で自然に通じる言い方なら ok=true にしてください。
- ユーザーの発話（音声の場合は発音・長音・促音・清濁・助詞も含めて）を聞き取り、次のJSONのみを出力してください（純粋なJSONのみ）：
{{
  "user_transcript": "ユーザーの発言の忠実な書き起こし（言い淀みや発音ミスも含める）",
  "ok": true,
  "verdict_cn": "一句话中文点评（例：『完全自然，意思和语气都很地道！』或『意思能通，但助词/动词搭配可以更自然』）",
  "better_jp": "ユーザーの言い方を活かした自然な修正版（ユーザーの言い方がそのままで自然なら空文字）",
  "pronunciation_tip_cn": "（音声の場合）発音・アクセント・長音・促音の注意点があれば中国語で1文。問題なければ空文字"
}}
"""


def scene_phrase_check(sid: str, target_cn: str, target_jp: str, user_text: str = "", audio_b64: str = "", audio_mime: str = "audio/webm") -> dict:
    cfg = load_config()
    scene_title = ""
    try:
        scene_title = _find_scene(sid).get("title", sid)
    except Exception:
        scene_title = sid or "日常生活"
    system = SCENE_PHRASE_CHECK_PROMPT.format(
        scene_title=scene_title,
        target_cn=target_cn,
        target_jp=target_jp,
    )
    parts = _scene_audio_parts(user_text, audio_b64, audio_mime)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    r = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    r.pop("_endpoint", None)
    if not audio_b64 and not r.get("user_transcript"):
        r["user_transcript"] = user_text.strip()
    return r


def scene_finish(sid: str, part: str, curveballs: list, history: list, fixes: list, audio_used: bool = False) -> dict:
    cfg = load_config()
    scene = _find_scene(sid)
    system = SCENE_FINISH_PROMPT.format(
        context=_scene_context(scene, part, curveballs),
        transcript=_history_text(history),
        fixes="\n".join(f"- {f.get('original', '')} → {f.get('better', '')}" for f in fixes or []) or "（なし）",
        problem_tag_ids=", ".join(f"{k}({v})" for k, v in PROBLEM_TAGS.items()),
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": "総評をお願いします。" + ("（発言の一部は音声でした）" if audio_used else "（発言はすべて文字入力でした）")}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    r = gemini_generate_json(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL", "gemini-3.8-flash"), payload)
    r.pop("_endpoint", None)
    rp = next((x for x in scene.get("roleplay", []) if x.get("part") == part), {})
    append_session_record({
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine": MACHINE,
        "scenario": "scene",
        "scene_id": sid,
        "scene_part": part,
        "model": cfg.get("GEMINI_MODEL", ""),
        "question": f"{scene.get('title')}／{rp.get('title', part)}",
        "input_type": "audio" if audio_used else "text",
        "user_transcript": " / ".join(h.get("jp", "") for h in history if h.get("role") == "me"),
        "dialogue": history,
        "curveballs": [c.get("situation_cn") for c in curveballs],
        "goal_achieved": bool(r.get("goal_achieved")),
        "scores": _clean_scores(r.get("scores")),
        "problem_tags": [t for t in (r.get("problem_tags") or []) if t in PROBLEM_TAGS],
        "one_line_progress_cn": r.get("one_line_progress_cn", ""),
        "error_breakdown": [{"original_part": f.get("original", ""), "fixed_part": f.get("better", ""), "reason_cn": f.get("reason_cn", "")} for f in fixes or []],
        "review_phrases": r.get("review_phrases", []),
        "critique_cn": "；".join(r.get("good_points_cn") or []),
    })
    return r


def scene_part_stats() -> dict:
    """每个场景每一段「任务完成」的次数：{scene_id: {part: n}}（给角色扮演自动开启意外情况用）"""
    out = {}
    for r in load_all_records():
        if r.get("scenario") == "scene" and r.get("goal_achieved") and r.get("scene_id") and r.get("scene_part"):
            d = out.setdefault(r["scene_id"], {})
            d[r["scene_part"]] = d.get(r["scene_part"], 0) + 1
    return out


def scenes_page() -> str:
    fp = APP_DIR / "scenes.html"
    return fp.read_text(encoding="utf-8") if fp.exists() else "<h1>scenes.html 缺失</h1>"


def bank_page() -> str:
    fp = APP_DIR / "bank.html"
    return fp.read_text(encoding="utf-8") if fp.exists() else "<h1>bank.html 缺失</h1>"


def progress_page() -> str:
    fp = APP_DIR / "progress.html"
    return fp.read_text(encoding="utf-8") if fp.exists() else "<h1>progress.html 缺失</h1>"


class CoachRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def _send_static(self, path: str):
        """图标与 manifest（static/ 目录，只读白名单内的文件名）"""
        static = Path(__file__).resolve().parent / "static"
        if path == "/manifest.webmanifest":
            body = json.dumps({
                "name": "日语口语私教台", "short_name": "口语私教台", "start_url": "/", "display": "standalone",
                "background_color": "#0f172a", "theme_color": "#0f172a",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
            }, ensure_ascii=False).encode("utf-8")
            ctype = "application/manifest+json"
        else:
            name = "favicon.ico" if path == "/favicon.ico" else path[len("/static/"):]
            fp = static / name
            if "/" in name or name.startswith(".") or not fp.is_file():
                self.send_error(404)
                return
            body = fp.read_bytes()
            ctype = {".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}.get(fp.suffix, "application/octet-stream")
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == "/" or path.startswith("/index"):
            body = HTML_PAGE.encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass
        elif path == "/favicon.ico" or path.startswith("/static/") or path == "/manifest.webmanifest":
            self._send_static(path)
        elif path.startswith("/bank"):
            self._send_html(bank_page())
        elif path.startswith("/api/bank"):
            try:
                self._send_json(bank_payload())
            except Exception as e:
                log_error("GET /api/bank", str(e))
                self._send_json({"error": str(e)}, status=500)
        elif path.startswith("/scenes"):
            self._send_html(scenes_page())
        elif path.startswith("/api/scenes"):
            try:
                self._send_json({"scenes": load_scenes(), "part_stats": scene_part_stats()})
            except Exception as e:
                log_error("GET /api/scenes", str(e))
                self._send_json({"error": str(e)}, status=500)
        elif path.startswith("/progress"):
            self._send_html(progress_page())
        elif path.startswith("/api/progress"):
            self._send_json({
                "records": load_all_records(),
                "tags": PROBLEM_TAGS,
                "score_keys": SCORE_KEYS,
                "machine": MACHINE,
                "record_dir": str(SESSION_DIR),
            })
        elif path == "/api/config":
            cfg = load_config()
            key = cfg.get("GEMINI_API_KEY", "")
            masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else ""
            self._send_json(
                {
                    "has_key": bool(key),
                    "masked_key": masked,
                    "model": cfg.get("GEMINI_MODEL", "gemini-3.8-flash"),
                    "latest_push": load_latest_daily_push(),
                }
            )
        else:
            self.send_error(404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        data = json.loads(raw)

        if self.path == "/api/config":
            cfg = load_config()
            new_key = data.get("api_key", "").strip() or cfg.get("GEMINI_API_KEY", "")
            new_model = data.get("model", "gemini-3.8-flash").strip()
            save_config(new_key, new_model)
            self._send_json({"message": "配置已保存至 ~/.credentials/google/jp_coach.env（密钥隔离保护）！"})
            return

        if self.path.startswith("/api/bank/") or self.path.startswith("/api/scene/") or self.path == "/api/tts":
            try:
                if self.path == "/api/scene/turn":
                    self._send_json({"result": scene_turn(
                        data["scene"], data.get("part", ""), data.get("curveballs", []), data.get("history", []),
                        data.get("user_text", ""), data.get("audio_b64", ""), data.get("audio_mime", "audio/webm"))})
                elif self.path == "/api/scene/finish":
                    self._send_json({"result": scene_finish(
                        data["scene"], data.get("part", ""), data.get("curveballs", []), data.get("history", []),
                        data.get("fixes", []), bool(data.get("audio_used")))})
                elif self.path == "/api/scene/phrase_check":
                    self._send_json({"result": scene_phrase_check(
                        data.get("scene", ""), data.get("target_cn", ""), data.get("target_jp", ""),
                        data.get("user_text", ""), data.get("audio_b64", ""), data.get("audio_mime", "audio/webm"))})
                elif self.path == "/api/scene/status":
                    sid = data.get("scene") or data.get("scene_id") or ""
                    self._send_json({"ok": True, "scene_status": update_scene_status(
                        sid, data.get("status", "active"), data.get("note", ""))})
                elif self.path == "/api/bank/generate":
                    self._send_json({"template": generate_template(data["id"])})
                elif self.path == "/api/bank/save":
                    self._send_json({"template": save_template_edit(data["id"], data.get("script_jp", ""))})
                elif self.path == "/api/bank/stage":
                    set_bank_stage(data["id"], int(data.get("stage", 1)))
                    self._send_json({"status": bank_status().get(data["id"], {"stage": int(data.get("stage", 1))})})
                elif self.path == "/api/bank/practice":
                    self._send_json({"result": practice_bank(
                        data["id"], int(data.get("stage", 1)), data.get("user_text", ""),
                        data.get("audio_b64", ""), data.get("audio_mime", "audio/webm"), data.get("company", ""),
                        data.get("ctx_question", ""), data.get("ctx_company", ""))})
                elif self.path == "/api/bank/ctxq":
                    self._send_json({"item": pick_context_question(data["id"], data.get("folder", ""))})
                elif self.path == "/api/bank/company/upsert":
                    self._send_json({"company": upsert_company(data.get("name", ""), data.get("folder", ""), data.get("memo", ""))})
                elif self.path == "/api/bank/company/delete":
                    delete_company(data["company"])
                    self._send_json({"ok": True})
                elif self.path == "/api/bank/company/generate":
                    self._send_json({"template": generate_company_template(data["company"], data["id"])})
                elif self.path == "/api/bank/company/save":
                    self._send_json({"template": save_company_template_edit(data["company"], data["id"], data.get("script_jp", ""))})
                elif self.path == "/api/tts":
                    cfg = load_config()
                    self._send_json({
                        "b64": synthesize_cloud_tts(
                            cfg.get("GEMINI_API_KEY", ""),
                            data.get("text", ""),
                            voice_name=data.get("voice", "ja-JP-NanamiNeural"),
                            speaking_rate=float(data.get("rate", 0.9)),
                        )
                    })
                else:
                    self.send_error(404)
            except Exception as e:
                log_error(f"POST {self.path} id={data.get('id')} audio={'yes' if data.get('audio_b64') else 'no'}", str(e))
                self._send_json({"error": str(e)}, status=500)
            return

        if self.path == "/api/daily/lock":
            try:
                res = lock_daily_to_ledger(data)
                self._send_json(res)
            except Exception as e:
                log_error("POST /api/daily/lock", str(e))
                self._send_json({"error": str(e)}, status=500)
            return

        if self.path == "/api/accumulate":
            jp = data.get("jp", "").strip()
            cn = data.get("cn", "").strip()
            if not jp:
                self._send_json({"error": "表达内容为空"}, status=400)
                return
            path_str = append_to_accum_md(jp, cn)
            self._send_json({"message": f"已追加至 {path_str}"})
            return

        if self.path == "/api/chat":
            cfg = load_config()
            api_key = cfg.get("GEMINI_API_KEY", "")
            model = data.get("model") or cfg.get("GEMINI_MODEL", "gemini-3.8-flash")
            scenario = data.get("scenario", "daily_push")
            custom_context = data.get("custom_context", "")
            history = data.get("history", [])
            user_text = data.get("user_text", "")
            audio_b64 = data.get("audio_b64", "")
            audio_mime = data.get("audio_mime", "audio/webm")
            is_opening = bool(data.get("is_opening", False))
            drill_same = bool(data.get("drill_same", True)) and not is_opening

            try:
                result = call_gemini_api(
                    api_key=api_key,
                    model=model,
                    scenario_key=scenario,
                    custom_context=custom_context,
                    history=history,
                    user_text=user_text,
                    audio_b64=audio_b64,
                    audio_mime=audio_mime,
                    drill_same=drill_same,
                )
                tts_b64 = synthesize_cloud_tts(api_key, result.get("reply_jp", ""), speaking_rate=0.88)
                result["tts_b64"] = tts_b64

                if not is_opening and result.get("user_transcript"):
                    assess = result.get("assessment") or {}
                    tags = [t for t in (assess.get("problem_tags") or []) if t in PROBLEM_TAGS]
                    question = ""
                    for turn in reversed(history):
                        if turn.get("role") != "user" and turn.get("text"):
                            question = turn["text"]
                            break
                    try:
                        append_session_record({
                            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "machine": MACHINE,
                            "scenario": scenario,
                            "model": model,
                            "question": question,
                            "input_type": "audio" if audio_b64 else "text",
                            "user_transcript": result.get("user_transcript", ""),
                            "scores": _clean_scores(assess.get("scores")),
                            "problem_tags": tags,
                            "one_line_progress_cn": assess.get("one_line_progress_cn", ""),
                            "error_breakdown": result.get("error_breakdown", []),
                            "unclear_pronunciations": (result.get("pronunciation_prosody") or {}).get("unclear_pronunciations", []),
                            "critique_cn": result.get("critique_cn", ""),
                            "polite_colleague_rewrite": result.get("polite_colleague_rewrite", ""),
                            "executive_keigo_rewrite": result.get("executive_keigo_rewrite", ""),
                            "key_vocab": result.get("key_vocab", []),
                        })
                    except OSError as e:
                        result["_save_error"] = str(e)

                self._send_json({"result": result, "_endpoint": result.get("_endpoint", "")})
            except Exception as e:
                log_error(f"/api/chat scenario={scenario} audio={'yes' if audio_b64 else 'no'} model={model}", str(e))
                self._send_json({"error": str(e)}, status=500)
            return

        self.send_error(404)



# ---------------------------------------------------------------------------
# 个人信息：放在同目录 local_profile.json（gitignored），代码里只留占位符和泛化默认值
# ---------------------------------------------------------------------------
LOCAL_PROFILE_FILE = Path(__file__).resolve().parent / "local_profile.json"
_PROFILE_DEFAULTS = {
    "candidate": "中国語母語話者の日本語上級学習者",
    "candidate_life": "中国語母語話者の日本語上級学習者",
    "research_hint": "個人素材に書かれた研究・経験",
    "scenarios": {},
    "scenario_labels": {
        "chem_rd": "🧪 场景 A：理工科 R&D 技术面试官（研究内容与社会实装深挖）",
        "it_ai_ds": "💻 场景 B：AI / 数据科学面试官（建模与跨界动机深挖）",
        "quant_finance": "📈 场景 C：金融机构面试官（市场与风险管理）",
    },
    "blocks_e": [
        {"label": "经历×成果", "jp": "【担当した工程】だけでなく、その先まで自分でやり切って、【定量的な成果】を出せました。"},
    ],
}


def load_local_profile() -> dict:
    prof = json.loads(json.dumps(_PROFILE_DEFAULTS))
    try:
        user = json.loads(LOCAL_PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(prof.get(k), dict):
            prof[k].update(v)
        else:
            prof[k] = v
    return prof


def _apply_local_profile() -> None:
    global SYSTEM_PROMPT_TEMPLATE, TEMPLATE_PROMPT, CTXQ_PROMPT, SCENE_TURN_PROMPT, HTML_PAGE
    import html as _html
    p = load_local_profile()
    subs = {
        "〔CANDIDATE〕": p["candidate"],
        "〔CANDIDATE_LIFE〕": p["candidate_life"],
        "〔RESEARCH_HINT〕": p["research_hint"],
    }
    for name in ("SYSTEM_PROMPT_TEMPLATE", "TEMPLATE_PROMPT", "CTXQ_PROMPT", "SCENE_TURN_PROMPT"):
        text = globals()[name]
        for k, v in subs.items():
            text = text.replace(k, v.replace("{", "{{").replace("}", "}}"))
        globals()[name] = text
    SCENARIOS.update({k: v for k, v in p.get("scenarios", {}).items() if v})
    page = HTML_PAGE
    for k, v in p["scenario_labels"].items():
        page = page.replace(f"〔LABEL_{k}〕", _html.escape(v))
    blocks = []
    for b in p.get("blocks_e", []):
        jp_text = str(b.get("jp", ""))
        js = jp_text.replace("\\", "\\\\").replace("'", "\\'")
        blocks.append(
            f"          • <b>[{_html.escape(str(b.get('label', '')))}]</b> <code>{_html.escape(jp_text)}</code>\n"
            f"          <button class=\"small-btn secondary\" onclick=\"speakJapanese('{_html.escape(js)}')\">🔊</button><br>"
        )
    HTML_PAGE = page.replace("〔BLOCKS_E〕", "\n" + "\n".join(blocks))


_apply_local_profile()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="日语口语私教台")
    parser.add_argument("--host", default=os.environ.get("JP_COACH_HOST", "127.0.0.1"), help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("JP_COACH_PORT", "0")), help="指定端口 (默认优先 18765)")
    parser.add_argument("--no-browser", action="store_true", help="后台常驻模式：启动时不自动弹出浏览器")
    args, _ = parser.parse_known_args()

    candidate_ports = [args.port] if args.port else [18765, 28765, 38765, 58765, 0]
    httpd = None
    for p in candidate_ports:
        try:
            httpd = http.server.ThreadingHTTPServer((args.host, p), CoachRequestHandler)
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"无法绑定端口: {candidate_ports}")

    actual_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    print("=" * 68, flush=True)
    print("  商务与求职日语实时私教台 已启动", flush=True)
    print(f"  本地访问地址: {url} (bind: {args.host}:{actual_port})", flush=True)
    print(f"  个人素材挂载: {PROFILE_MD}", flush=True)
    print(f"  进步记录    : {SESSION_DIR}  (本机: {MACHINE})", flush=True)
    print(f"  进步追踪页  : {url}/progress", flush=True)
    print(f"  面试题库    : {url}/bank", flush=True)
    print(f"  生活场景    : {url}/scenes", flush=True)
    print("  按 Ctrl+C 即可随时关闭服务", flush=True)
    print("=" * 68, flush=True)
    if not args.no_browser and not os.environ.get("JP_COACH_NO_BROWSER"):
        webbrowser.open(url)
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。")


if __name__ == "__main__":
    main()
