#!/usr/bin/env python3
"""列出「网页上标注了、或资料更新了，需要 Claude 在对话里处理」的事项。

用法：python check_updates.py           # 列出待办（不改任何东西）
      python check_updates.py --mark    # 列出后把「资料有更新」的检查点记到现在（处理完再跑）

检查项：
  1. 生活场景标了「待 Archive」→ 归档并写新场景
  2. 面试单元打卡后，下一个 Day 还没写
  3. 个人素材改过 → 通用模板 / 公司版模板过时（profile_hash 对不上）
  4. 02_企業別 出现新公司文件夹，还没分日语 / 英语（company_lang）
  5. 上次检查之后，个人素材、积累库、02_企業別 里有文件更新
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jp_coach as jc  # noqa: E402

STAMP = jc.PROJECT_ROOT / ".claude_last_check"


def profile_hash() -> str:
    return jc.profile_hash()  # 和网页用同一个算法（按读进来的素材文本算），别自己另算


def main() -> int:
    todo = []

    # 1. 生活场景待归档
    marked = [s for s in jc.load_scenes() if s.get("status") == "marked_archive"]
    for s in marked:
        todo.append(f"🗂 生活场景「{s.get('title')}」标了待 Archive（{s.get('marked_at', '')}）→ 跑 manage_life_scenes.py archive-marked，再写 1 个新场景")

    # 2. 面试单元
    prog = jc.interview_progress()
    if not prog["current_unit_ready"]:
        todo.append(f"🎯 面试单元 Day {prog['current_day']} 还没写（已打卡 {len(prog['checked_days'])} 天）")

    # 3. 模板过时
    ph = profile_hash()
    stale = [q["id"] for q in jc.load_bank().get("questions", [])
             if q.get("template") and q["template"].get("profile_hash") not in ("", None, ph) and not q["template"].get("edited")]
    if stale:
        todo.append(f"📝 个人素材改过，{len(stale)} 道通用模板是按旧素材写的：{', '.join(stale[:8])}{' …' if len(stale) > 8 else ''}")
    for folder, c in jc.load_company_bank().get("companies", {}).items():
        old = [qid for qid, t in (c.get("templates") or {}).items() if t.get("profile_hash") not in ("", None, ph) and not t.get("edited")]
        if old:
            todo.append(f"📝 {folder} 的公司版模板有 {len(old)} 道是按旧素材写的：{', '.join(old)}")

    # 4. 新公司没分语言
    comp_dir = jc.PROJECT_ROOT / "02_企業別"
    lang = jc.load_ctxq().get("company_lang", {})
    if comp_dir.exists():
        new = sorted(d.name for d in comp_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")) and d.name not in lang)
        if new:
            todo.append(f"🏢 02_企業別 有新公司还没分日语/英语面试：{', '.join(new)}（分好后可以补背景题）")

    # 5. 上次检查之后更新过的资料
    since = STAMP.stat().st_mtime if STAMP.exists() else 0
    changed = []
    for fp in [jc.SPEAKING_DIR / "个人素材.md", jc.SPEAKING_DIR / "积累库.md"]:
        if fp.exists() and fp.stat().st_mtime > since:
            changed.append(fp.name)
    if comp_dir.exists():
        for fp in comp_dir.rglob("*"):
            if fp.is_file() and fp.suffix.lower() in (".md", ".txt") and fp.stat().st_mtime > since and "_archive" not in fp.parts:
                changed.append(str(fp.relative_to(comp_dir)))
    if changed and since:
        when = datetime.fromtimestamp(since).strftime("%m-%d %H:%M")
        todo.append(f"📄 {when} 之后更新过的资料（{len(changed)} 个）：{', '.join(changed[:10])}{' …' if len(changed) > 10 else ''}")

    print("\n".join(todo) if todo else "✅ 没有需要处理的更新")
    if not since:
        print("（第一次运行：还没有检查点。处理完后跑 --mark，之后只列新的资料更新）")
    if "--mark" in sys.argv:
        STAMP.touch()
        print("已把资料检查点记到现在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
