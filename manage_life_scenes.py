#!/usr/bin/env python3
"""日本生活高频口语场景库管理工具 (manage_life_scenes.py)

功能：
1. status          : 查看当前在练 (active)、待归档 (marked_archive)、已归档 (archived) 场景列表，以及未覆盖的新场景候选池。
2. archive-marked  : 将所有标注为 marked_archive 的场景正式移入 archive/ 目录（同时同步本地 SSD 与 OneDrive），并输出需要补充的新场景数量与候选主题。
3. archive <id>    : 指定将某个场景 ID 立即归档至 archive/。
4. unarchive <id>  : 将某个已归档的场景移回在练列表 (active)。
5. validate <path> : 校验新生成的生活场景 JSON 结构及台词纯净度（检查是否混入括号舞台提示），并自动同步至本地 SSD 与 OneDrive。
"""

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

LOCAL_APP_DIR = Path(__file__).resolve().parent
LOCAL_SCENE_DIR = LOCAL_APP_DIR / "data/06_日本語口語/生活场景"
LOCAL_ARCH_DIR = LOCAL_SCENE_DIR / "archive"
STATE_FILE = LOCAL_SCENE_DIR / "_scene_state.json"

sys.path.insert(0, str(LOCAL_APP_DIR))
from sync_onedrive import ONEDRIVE_ROOT  # noqa: E402  与同步器共用同一个 OneDrive 根目录
ONEDRIVE_SCENE_DIR = ONEDRIVE_ROOT / "06_日本語口語/生活场景"
ONEDRIVE_ARCH_DIR = ONEDRIVE_SCENE_DIR / "archive"

# 精选在日生活高频实用场景候选池（按紧迫度与实用度排序）
CANDIDATE_TOPICS = [
    {
        "id": "drugstore_otc",
        "category": "医疗・健康",
        "icon": "💊",
        "title": "药妆店买药：向药剂师描述症状与确认第1类医药品",
        "subtitle": "偏头痛、胃痛或过敏发作时，在药局柜台说明症状、服药史并听取用法用量",
    },
    {
        "id": "train_lost_item",
        "category": "出行・交通",
        "icon": "🚃",
        "title": "车站遗失物查询：电车忘拿背包/伞的挂失与认领",
        "subtitle": "向改札口站员说明乘车路线、车厢位置、物品特征及联系领取手续",
    },
    {
        "id": "bank_card_trouble",
        "category": "金融・行政",
        "icon": "💳",
        "title": "银行与信用卡客服：大额消费风控解锁与地址变更",
        "subtitle": "信用卡买机票/家电触发风控锁卡时的电话核身、临时提额与账户手续",
    },
    {
        "id": "izakaya_reservation",
        "category": "日常服务",
        "icon": "🍻",
        "title": "餐厅与居酒屋：电话预约、忌口说明与临时变更人数",
        "subtitle": "预约包间/吧台、确认畅饮套餐规则、朋友临时迟到或减少人数的得体沟通",
    },
    {
        "id": "mobile_mnp_plan",
        "category": "居家生活",
        "icon": "📱",
        "title": "手机与光纤签约：MNP 携号转网、套餐变更与解约确认",
        "subtitle": "在运营商柜台办理番号移行、取消多余收费选项、确认违约金与光纤工事日",
    },
    {
        "id": "move_out_inspection",
        "category": "居家生活",
        "icon": "🔑",
        "title": "退租立会检查：房间折旧说明与原状回复费用交涉",
        "subtitle": "搬家退房时与管理公司立会负责人核对划痕、自然老化边界及敷金结算",
    },
    {
        "id": "eye_clinic_contacts",
        "category": "医疗・健康",
        "icon": "👓",
        "title": "眼科与配镜：隐形眼镜处方检查、散光调整与干眼咨询",
        "subtitle": "在眼科做视力与角膜检查、试戴镜片反馈清晰度与异物感、开具处方笺",
    },
    {
        "id": "gym_membership",
        "category": "日常服务",
        "icon": "🏋️",
        "title": "健身房見学与入会：器械区规则、休会与退会手续",
        "subtitle": "预约体验参观、询问24小时门禁与室内鞋规定、办理出差休会或退会",
    },
    {
        "id": "city_tax_mynumber",
        "category": "行政・手续",
        "icon": "🪪",
        "title": "区役所个人番号卡：电子证明书更新、密码解锁与住民税咨询",
        "subtitle": "办理マイナンバーカード在留期限延长更新、重设锁定的署名用密码及纳税证明",
    },
    {
        "id": "bicycle_impound",
        "category": "出行・交通",
        "icon": "🚲",
        "title": "自行车撤去领回与维修：保管所认领、防犯登录与补胎",
        "subtitle": "在撤去自行车保管所出示证件缴费领车，或在自行车行修理爆胎与刹车",
    },
]

STAGE_NOTE_RE = re.compile(r"([（(][^）)]*[）)]|【[^】]*】)")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("scenes"), dict):
                    return raw["scenes"]
                return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return {}
    return {}


def save_state(scenes_map: dict) -> None:
    LOCAL_SCENE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"scenes": scenes_map}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get_all_scenes() -> dict:
    state = load_state()
    scenes = {}
    LOCAL_SCENE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_ARCH_DIR.mkdir(parents=True, exist_ok=True)

    for fp in sorted(LOCAL_SCENE_DIR.glob("*.json")):
        if fp.name.startswith("_"):
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            sid = data.get("id") or fp.stem
            st_meta = state.get(sid, {})
            st = st_meta.get("status", "active")
            if st == "archived":
                st = "active"
            scenes[sid] = {
                "id": sid,
                "title": data.get("title", sid),
                "category": data.get("category", ""),
                "icon": data.get("icon", "🗣️"),
                "status": st,
                "updated_at": st_meta.get("updated_at", ""),
                "path": str(fp),
                "location": "active_dir",
            }
        except Exception as e:
            print(f"[WARN] 读取 {fp} 失败: {e}", file=sys.stderr)

    for fp in sorted(LOCAL_ARCH_DIR.glob("*.json")):
        if fp.name.startswith("_"):
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            sid = data.get("id") or fp.stem
            if sid in scenes:
                continue
            st_meta = state.get(sid, {})
            scenes[sid] = {
                "id": sid,
                "title": data.get("title", sid),
                "category": data.get("category", ""),
                "icon": data.get("icon", "🗣️"),
                "status": "archived",
                "updated_at": st_meta.get("updated_at", ""),
                "path": str(fp),
                "location": "archive_dir",
            }
        except Exception as e:
            print(f"[WARN] 读取 {fp} 失败: {e}", file=sys.stderr)

    return scenes


def archive_scene_by_id(sid: str, state: dict) -> bool:
    LOCAL_ARCH_DIR.mkdir(parents=True, exist_ok=True)
    src = LOCAL_SCENE_DIR / f"{sid}.json"
    dst = LOCAL_ARCH_DIR / f"{sid}.json"
    moved = False
    if src.exists():
        shutil.move(str(src), str(dst))
        moved = True
    elif dst.exists():
        moved = True

    if ONEDRIVE_SCENE_DIR.exists():
        ONEDRIVE_ARCH_DIR.mkdir(parents=True, exist_ok=True)
        od_src = ONEDRIVE_SCENE_DIR / f"{sid}.json"
        od_dst = ONEDRIVE_ARCH_DIR / f"{sid}.json"
        if od_src.exists():
            shutil.move(str(od_src), str(od_dst))
        elif dst.exists() and not od_dst.exists():
            shutil.copy2(str(dst), str(od_dst))

    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    prev = state.get(sid, {})
    state[sid] = {
        "status": "archived",
        "updated_at": now_iso,
        "archived_at": now_iso,
        "note": prev.get("note", ""),
    }
    return moved


def unarchive_scene_by_id(sid: str, state: dict) -> bool:
    src = LOCAL_ARCH_DIR / f"{sid}.json"
    dst = LOCAL_SCENE_DIR / f"{sid}.json"
    moved = False
    if src.exists():
        shutil.move(str(src), str(dst))
        moved = True
    elif dst.exists():
        moved = True

    if ONEDRIVE_ARCH_DIR.exists():
        od_src = ONEDRIVE_ARCH_DIR / f"{sid}.json"
        od_dst = ONEDRIVE_SCENE_DIR / f"{sid}.json"
        if od_src.exists():
            shutil.move(str(od_src), str(od_dst))

    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    state[sid] = {
        "status": "active",
        "updated_at": now_iso,
    }
    return moved


def run_sync_script() -> None:
    sync_py = LOCAL_APP_DIR / "sync_onedrive.py"
    if sync_py.exists():
        subprocess.run([sys.executable, str(sync_py)], check=False)


def cmd_status(args: argparse.Namespace) -> int:
    scenes = get_all_scenes()
    active = [s for s in scenes.values() if s["status"] == "active"]
    marked = [s for s in scenes.values() if s["status"] == "marked_archive"]
    archived = [s for s in scenes.values() if s["status"] == "archived"]
    available_candidates = [c for c in CANDIDATE_TOPICS if c["id"] not in scenes]

    if args.json:
        print(json.dumps({
            "active": active,
            "marked_archive": marked,
            "archived": archived,
            "available_candidates": available_candidates,
        }, ensure_ascii=False, indent=2))
        return 0

    print("=== 🗾 日本生活场景口语库状态 ===")
    print(f"\n📌 在练场景 (active: {len(active)})")
    for s in active:
        print(f"  - [{s['id']}] {s['icon']} {s['category']} | {s['title']}")

    print(f"\n🏷️ 待归档场景 (marked_archive: {len(marked)})")
    if marked:
        for s in marked:
            print(f"  - [{s['id']}] {s['icon']} {s['category']} | {s['title']} (标记于: {s['updated_at']})")
    else:
        print("  (当前没有标记为待归档的场景)")

    print(f"\n📦 已归档场景库 (archived: {len(archived)} · 可在网页侧边栏底部随时调取)")
    if archived:
        for s in archived:
            print(f"  - [{s['id']}] {s['icon']} {s['category']} | {s['title']}")
    else:
        print("  (暂无已归档场景)")

    print(f"\n🌱 可生成的新场景候选池 (剩余 {len(available_candidates)} 个预设主题):")
    for c in available_candidates[:5]:
        print(f"  - [{c['id']}] {c['icon']} {c['category']} | {c['title']} — {c['subtitle']}")
    return 0


def cmd_archive_marked(args: argparse.Namespace) -> int:
    scenes = get_all_scenes()
    state = load_state()
    marked = [s for s in scenes.values() if s["status"] == "marked_archive"]

    if not marked:
        print("当前没有标记为「待 Archive」(marked_archive) 的生活场景，无需归档。")
        return 0

    archived_list = []
    for s in marked:
        if archive_scene_by_id(s["id"], state):
            archived_list.append(s)
            print(f"✅ 已归档: [{s['id']}] {s['icon']} {s['title']} -> archive/{s['id']}.json")

    save_state(state)
    run_sync_script()

    all_after = get_all_scenes()
    candidates = [c for c in CANDIDATE_TOPICS if c["id"] not in all_after]
    need_count = len(archived_list)
    print(f"\n🎯 本次共归档 {need_count} 个场景。请补充生成 {need_count} 个新生活场景到 {LOCAL_SCENE_DIR}：")
    for idx, c in enumerate(candidates[:need_count], 1):
        print(f"  推荐新场景 #{idx}: id={c['id']} | {c['icon']} {c['category']} | {c['title']} ({c['subtitle']})")
    return 0


def cmd_archive_one(args: argparse.Namespace) -> int:
    state = load_state()
    sid = args.scene_id.strip()
    if archive_scene_by_id(sid, state):
        save_state(state)
        run_sync_script()
        print(f"✅ 已将场景 [{sid}] 归档至 archive/{sid}.json 并同步状态。")
        return 0
    print(f"❌ 未找到场景 [{sid}]", file=sys.stderr)
    return 1


def cmd_unarchive_one(args: argparse.Namespace) -> int:
    state = load_state()
    sid = args.scene_id.strip()
    if unarchive_scene_by_id(sid, state):
        save_state(state)
        run_sync_script()
        print(f"✅ 已将场景 [{sid}] 移回在练列表 (active) 并同步状态。")
        return 0
    print(f"❌ 未找到已归档场景 [{sid}]", file=sys.stderr)
    return 1


def cmd_validate(args: argparse.Namespace) -> int:
    fp = Path(args.json_path).expanduser().resolve()
    if not fp.exists():
        print(f"❌ 文件不存在: {fp}", file=sys.stderr)
        return 1

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
        return 1

    required_top = ["id", "category", "icon", "title", "subtitle", "situation_cn", "parts", "phrases", "vocab", "curveballs", "tips_cn", "roleplay"]
    missing = [k for k in required_top if k not in data]
    if missing:
        print(f"❌ 缺少必填顶层字段: {missing}", file=sys.stderr)
        return 1

    sid = data["id"]
    warnings = []
    for p in data.get("parts", []):
        for idx, line in enumerate(p.get("lines", [])):
            jp = line.get("jp", "")
            if STAGE_NOTE_RE.search(jp):
                warnings.append(f"Part {p.get('id')} 第 {idx+1} 句 jp 含括号说明: {jp}")

    if warnings:
        print("⚠️ 检测到以下台词包含括号说明（建议将舞台动作放入 cn 翻译，保持 jp 纯净口语，不过 TTS 已做自动过滤）：")
        for w in warnings:
            print(f"  - {w}")

    target_fp = LOCAL_SCENE_DIR / f"{sid}.json"
    if fp != target_fp:
        shutil.copy2(str(fp), str(target_fp))

    state = load_state()
    state[sid] = {
        "status": "active",
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    save_state(state)
    run_sync_script()
    print(f"✅ 场景 [{sid}] ({data['title']}) 校验通过，已安装至 {target_fp} 并同步至 OneDrive！")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="日本生活场景口语库管理工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser("status", help="查看所有场景状态与候选新场景")
    p_st.add_argument("--json", action="store_true", help="输出 JSON 格式")
    p_st.set_defaults(func=cmd_status)

    p_am = sub.add_parser("archive-marked", help="将所有标记为 marked_archive 的场景归档")
    p_am.set_defaults(func=cmd_archive_marked)

    p_ar = sub.add_parser("archive", help="归档指定场景 ID")
    p_ar.add_argument("scene_id", help="场景 ID")
    p_ar.set_defaults(func=cmd_archive_one)

    p_un = sub.add_parser("unarchive", help="将指定已归档场景移回在练列表")
    p_un.add_argument("scene_id", help="场景 ID")
    p_un.set_defaults(func=cmd_unarchive_one)

    p_val = sub.add_parser("validate", help="校验并安装新生活场景 JSON")
    p_val.add_argument("json_path", help="场景 JSON 文件路径")
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
