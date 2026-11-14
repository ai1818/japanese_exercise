#!/usr/bin/env python3
"""日语口语私教台：本地硬盘 (~/Projects/日语口语私教台) 与 OneDrive 资料库 异步双向同步器。

设计目标：
1. 私教台 Web 服务 (jp_coach.py) 100% 读写本机 SSD (~/Projects/日语口语私教台/data/)，完全不直接接触 OneDrive；
   即便 OneDrive 崩溃、退登、占位符死锁，Web 服务也零感知、零卡顿。
2. 本脚本负责在后台静默同步：
   - 单向拉取（OneDrive -> 本地 data/）：
     * 01_自分の材料/自己分析_PR/Master_Facts.md
     * 02_企業別/ 下的公司目录结构及 .md / .txt 调研与面试资料
     * 06_日本語口語/个人素材.md
     * 06_日本語口語/今日推送/*.md
     * 06_日本語口語/生活场景/*.json
     * 06_日本語口語/記録/dialog_review_notes.csv
   - 双向同步（本地 <-> OneDrive，按最新修改时间 / 只增不减原则）：
     * 06_日本語口語/面试题库.json
     * 06_日本語口語/面试题库_公司版.json
     * 06_日本語口語/面试题库_背景题.json
     * 06_日本語口語/积累库.md
     * 06_日本語口語/記録/coach_sessions/**/*.jsonl
     * 05_ツール/speaking_coach/ 下的 jp_coach.py, bank.html, scenes.html, progress.html
3. 严守安全红线：只做新增与新版本覆盖，绝不删除 OneDrive 上的任何文件。
"""

import ctypes
import datetime
import fcntl
import json
import os
import shutil
import sys
from pathlib import Path

LOCAL_APP_DIR = Path(__file__).resolve().parent
LOCAL_DATA_ROOT = LOCAL_APP_DIR / "data"

def _onedrive_root() -> Path:
    """OneDrive 资料库根目录：环境变量 JP_JOB_HUNT_ONEDRIVE > local_profile.json 的 onedrive_root > ~/OneDrive/jp_coach_data"""
    env = os.environ.get("JP_JOB_HUNT_ONEDRIVE", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        import json
        prof = json.loads((Path(__file__).resolve().parent / "local_profile.json").read_text(encoding="utf-8"))
        if prof.get("onedrive_root"):
            return Path(prof["onedrive_root"]).expanduser()
    except (OSError, ValueError):
        pass
    return Path.home() / "OneDrive" / "jp_coach_data"


ONEDRIVE_ROOT = _onedrive_root()
ONEDRIVE_APP_DIR = ONEDRIVE_ROOT / "05_ツール" / "speaking_coach"
LOCK_FILE = Path("/tmp/jp_coach_sync.lock")
QUIET = "--quiet" in sys.argv


def log(msg: str) -> None:
    if not QUIET:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


def allow_dataless_materialization() -> None:
    """开启 macOS 进程级 dataless 占位符自动下载策略，避免后台进程读 OneDrive 报 Errno 11 死锁。"""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.getiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.setiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        TYPE_MATERIALIZE_DATALESS, SCOPE_PROCESS, ON = 3, 0, 2
        if libc.getiopolicy_np(TYPE_MATERIALIZE_DATALESS, SCOPE_PROCESS) != ON:
            libc.setiopolicy_np(TYPE_MATERIALIZE_DATALESS, SCOPE_PROCESS, ON)
    except Exception:
        pass


def atomic_copy(src: Path, dst: Path) -> bool:
    """安全复制文件并保留时间戳；遇到 OneDrive 超时或占位符异常时静默跳过，不破坏目标文件。"""
    try:
        if not src.exists():
            return False
        data = src.read_bytes()
        st = src.stat()
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.sync_tmp_{os.getpid()}")
        tmp.write_bytes(data)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, dst)
        return True
    except OSError as e:
        log(f"跳过 {src} -> {dst}: {e}")
        return False


def sync_one_way(src: Path, dst: Path) -> int:
    """单向同步：当 src 存在且比 dst 新（或大小不同）时复制到 dst。"""
    try:
        if not src.exists():
            return 0
        if dst.exists():
            s_stat, d_stat = src.stat(), dst.stat()
            if abs(s_stat.st_mtime - d_stat.st_mtime) <= 1.5 and s_stat.st_size == d_stat.st_size:
                return 0
        if atomic_copy(src, dst):
            log(f"拉取 OneDrive -> 本地: {dst.relative_to(LOCAL_APP_DIR)}")
            return 1
    except OSError as e:
        log(f"检查单向同步失败 {src}: {e}")
    return 0


def sync_two_way(local_fp: Path, remote_fp: Path, append_only: bool = False) -> int:
    """双向同步：比较本地与 OneDrive 的修改时间（或 append_only 文件大小），将较新版本同步给另一方。"""
    try:
        l_exists = local_fp.exists()
        r_exists = remote_fp.exists()
        if not l_exists and not r_exists:
            return 0
        if l_exists and not r_exists:
            if atomic_copy(local_fp, remote_fp):
                log(f"推送 本地 -> OneDrive (新增): {local_fp.name}")
                return 1
            return 0
        if r_exists and not l_exists:
            if atomic_copy(remote_fp, local_fp):
                log(f"拉取 OneDrive -> 本地 (新增): {remote_fp.name}")
                return 1
            return 0

        l_stat = local_fp.stat()
        r_stat = remote_fp.stat()

        if append_only:
            # 对于 .jsonl 和 积累库.md，优先保护内容更多的一方
            if l_stat.st_size > r_stat.st_size:
                if atomic_copy(local_fp, remote_fp):
                    log(f"推送 本地 -> OneDrive (追加更新): {local_fp.name}")
                    return 1
            elif r_stat.st_size > l_stat.st_size:
                if atomic_copy(remote_fp, local_fp):
                    log(f"拉取 OneDrive -> 本地 (追加更新): {remote_fp.name}")
                    return 1
            return 0

        # 普通文件（JSON 题库、代码、HTML）：比较 mtime（2 秒容差）
        if l_stat.st_mtime > r_stat.st_mtime + 2.0:
            if atomic_copy(local_fp, remote_fp):
                log(f"推送 本地 -> OneDrive (更新): {local_fp.name}")
                return 1
        elif r_stat.st_mtime > l_stat.st_mtime + 2.0:
            if atomic_copy(remote_fp, local_fp):
                log(f"拉取 OneDrive -> 本地 (更新): {remote_fp.name}")
                return 1
    except OSError as e:
        log(f"双向同步异常 {local_fp.name}: {e}")
    return 0


def run_sync() -> int:
    allow_dataless_materialization()
    LOCAL_DATA_ROOT.mkdir(parents=True, exist_ok=True)

    if not ONEDRIVE_ROOT.exists():
        log(f"OneDrive 目录当前不可用 ({ONEDRIVE_ROOT})，跳过同步。")
        return 0

    changed = 0

    # 1. 应用代码与前端页面双向同步
    for fname in ("jp_coach.py", "bank.html", "scenes.html", "interview.html", "progress.html", "manage_life_scenes.py", "sync_onedrive.py", "README.md", ".gitignore", "local_profile.json"):
        changed += sync_two_way(LOCAL_APP_DIR / fname, ONEDRIVE_APP_DIR / fname, append_only=False)

    # 2. 单向拉取外部源数据（OneDrive -> 本地 data/）
    one_way_files = [
        Path("01_自分の材料/自己分析_PR/Master_Facts.md"),
        Path("06_日本語口語/个人素材.md"),
        Path("06_日本語口語/記録/dialog_review_notes.csv"),
    ]
    for rel in one_way_files:
        changed += sync_one_way(ONEDRIVE_ROOT / rel, LOCAL_DATA_ROOT / rel)

    # 今日推送/*.md 单向拉取
    src_dir = ONEDRIVE_ROOT / "06_日本語口語/今日推送"
    dst_dir = LOCAL_DATA_ROOT / "06_日本語口語/今日推送"
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        if src_dir.exists():
            for fp in src_dir.glob("*.md"):
                if fp.is_file():
                    changed += sync_one_way(fp, dst_dir / fp.name)
    except OSError as e:
        log(f"扫描 {src_dir} 失败: {e}")

    # 生活场景/*.json、_scene_state.json 与 archive/*.json 双向同步
    l_scene_dir = LOCAL_DATA_ROOT / "06_日本語口語/生活场景"
    r_scene_dir = ONEDRIVE_ROOT / "06_日本語口語/生活场景"
    l_arch_dir = l_scene_dir / "archive"
    r_arch_dir = r_scene_dir / "archive"
    l_scene_dir.mkdir(parents=True, exist_ok=True)
    l_arch_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. 先同步状态表 _scene_state.json
        changed += sync_two_way(l_scene_dir / "_scene_state.json", r_scene_dir / "_scene_state.json", append_only=False)
        scene_states = {}
        state_fp = l_scene_dir / "_scene_state.json"
        if state_fp.exists():
            try:
                raw_st = json.loads(state_fp.read_text(encoding="utf-8"))
                if isinstance(raw_st, dict):
                    scene_states = raw_st.get("scenes", raw_st) if isinstance(raw_st.get("scenes"), dict) else raw_st
            except Exception:
                scene_states = {}

        # 2. 按 _scene_state.json 的状态先对齐两侧目录位置（active/marked_archive 在顶层，archived 在 archive/）
        for sid, meta in scene_states.items():
            if not isinstance(meta, dict):
                continue
            st = meta.get("status", "active")
            fname = f"{sid}.json"
            if st == "archived":
                for top_dir, sub_arch_dir in ((l_scene_dir, l_arch_dir), (r_scene_dir, r_arch_dir)):
                    src_f = top_dir / fname
                    dst_f = sub_arch_dir / fname
                    if src_f.exists() and src_f.is_file():
                        sub_arch_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src_f), str(dst_f))
                        log(f"[状态对齐->归档] {src_f} -> {dst_f}")
                        changed += 1
            elif st in ("active", "marked_archive"):
                for top_dir, sub_arch_dir in ((l_scene_dir, l_arch_dir), (r_scene_dir, r_arch_dir)):
                    src_f = sub_arch_dir / fname
                    dst_f = top_dir / fname
                    if src_f.exists() and src_f.is_file():
                        top_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src_f), str(dst_f))
                        log(f"[状态对齐->在练] {src_f} -> {dst_f}")
                        changed += 1

        # 3. 同步 archive/*.json
        arch_names = set()
        if l_arch_dir.exists():
            arch_names.update(p.name for p in l_arch_dir.glob("*.json") if p.is_file())
        if r_arch_dir.exists():
            arch_names.update(p.name for p in r_arch_dir.glob("*.json") if p.is_file())
        for aname in sorted(arch_names):
            changed += sync_two_way(l_arch_dir / aname, r_arch_dir / aname, append_only=False)

        # 4. 若某场景已进入 archive/，将两侧顶层目录中残留的同名文件归入 archive/，防止双向同步将其复活回在练目录
        for aname in sorted(arch_names):
            for top_dir, sub_arch_dir in ((l_scene_dir, l_arch_dir), (r_scene_dir, r_arch_dir)):
                lingering = top_dir / aname
                if lingering.exists() and lingering.is_file():
                    sub_arch_dir.mkdir(parents=True, exist_ok=True)
                    target_arch = sub_arch_dir / aname
                    if not target_arch.exists():
                        shutil.move(str(lingering), str(target_arch))
                    else:
                        lingering.unlink()
                    log(f"[归档清理] 已移除顶层已归档残留文件: {lingering}")
                    changed += 1

        # 4. 同步在练 生活场景/*.json
        scene_names = set()
        if l_scene_dir.exists():
            scene_names.update(p.name for p in l_scene_dir.glob("*.json") if p.is_file() and p.name != "_scene_state.json")
        if r_scene_dir.exists():
            scene_names.update(p.name for p in r_scene_dir.glob("*.json") if p.is_file() and p.name != "_scene_state.json")
        for sname in sorted(scene_names):
            if sname in arch_names:
                continue
            changed += sync_two_way(l_scene_dir / sname, r_scene_dir / sname, append_only=False)
    except OSError as e:
        log(f"同步 生活场景 失败: {e}")

    # 02_企業別/ 下的所有公司目录及 .md/.txt 资料
    od_comp_dir = ONEDRIVE_ROOT / "02_企業別"
    loc_comp_dir = LOCAL_DATA_ROOT / "02_企業別"
    loc_comp_dir.mkdir(parents=True, exist_ok=True)
    try:
        if od_comp_dir.exists():
            for d in od_comp_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    (loc_comp_dir / d.name).mkdir(parents=True, exist_ok=True)
                    for fp in d.rglob("*"):
                        if fp.is_file() and fp.suffix.lower() in (".md", ".txt"):
                            rel = fp.relative_to(od_comp_dir)
                            changed += sync_one_way(fp, loc_comp_dir / rel)
    except OSError as e:
        log(f"同步 02_企業別 失败: {e}")

    # 3. 双向同步核心读写数据（本地 data/ <-> OneDrive）
    for rel, is_append in [
        (Path("06_日本語口語/面试题库.json"), False),
        (Path("06_日本語口語/面试题库_公司版.json"), False),
        (Path("06_日本語口語/面试题库_背景题.json"), False),
        (Path("06_日本語口語/积累库.md"), True),
        (Path("06_日本語口語/口語訓練記録台帳.md"), True),
    ]:
        changed += sync_two_way(LOCAL_DATA_ROOT / rel, ONEDRIVE_ROOT / rel, append_only=is_append)

    # 面试单元 day_NN.json（Claude 在对话里写）双向；打卡记录 _checkins_<电脑名>.jsonl 在下面按只追加同步
    iv = Path("06_日本語口語/面试单元")
    try:
        (LOCAL_DATA_ROOT / iv).mkdir(parents=True, exist_ok=True)
        names = {fp.name for d in (LOCAL_DATA_ROOT / iv, ONEDRIVE_ROOT / iv) if d.exists() for fp in d.glob("day_*.json")}
        for name in sorted(names):
            changed += sync_two_way(LOCAL_DATA_ROOT / iv / name, ONEDRIVE_ROOT / iv / name, append_only=False)
    except OSError as e:
        log(f"同步 {iv} 失败: {e}")

    # coach_sessions/*.jsonl 及 coach_sessions/bank_stage/*.jsonl
    for rel_sub in [
        Path("06_日本語口語/記録/coach_sessions"),
        Path("06_日本語口語/記録/coach_sessions/bank_stage"),
        Path("06_日本語口語/面试单元"),
    ]:
        l_dir = LOCAL_DATA_ROOT / rel_sub
        r_dir = ONEDRIVE_ROOT / rel_sub
        l_dir.mkdir(parents=True, exist_ok=True)
        try:
            names = set()
            if l_dir.exists():
                names.update(p.name for p in l_dir.glob("*.jsonl") if p.is_file())
            if r_dir.exists():
                names.update(p.name for p in r_dir.glob("*.jsonl") if p.is_file())
            for name in sorted(names):
                changed += sync_two_way(l_dir / name, r_dir / name, append_only=True)
        except OSError as e:
            log(f"同步 {rel_sub} 失败: {e}")

    log(f"同步完成，共更新 {changed} 个文件。")
    return changed


def main() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有另一个同步进程在运行，本次跳过。")
            return
        run_sync()


if __name__ == "__main__":
    main()
