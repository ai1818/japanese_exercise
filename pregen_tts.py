#!/usr/bin/env python3
"""把面试单元和生活场景里的台词提前生成成语音，存进 data/tts_cache/。

网页点播放时，服务器先查这个缓存；命中就直接返回文件，不用临场去请求微软的语音服务。
只生成默认设置（自动分角色音色、语速 0.9x）下会用到的版本；换了音色或语速的，第一次点还是现生成，之后同样会缓存。

用法：python pregen_tts.py            # 全部
      python pregen_tts.py iv_day01   # 只生成某个单元/场景
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jp_coach as jc  # noqa: E402

RATE = 0.9  # 网页默认语速
MALE = {"doctor", "driver", "manager", "me"}  # 和 scenes.html / interview.html 的 resolveVoice 自动分角色一致


def voice_for(who: str) -> str:
    return "keita" if who in MALE else "nanami"


def items_for(unit: dict) -> list:
    out = []
    for p in unit.get("parts", []):
        for line in p.get("lines", []):
            out.append((line.get("jp", ""), voice_for(line.get("who", "staff"))))
    for ph in unit.get("phrases", []):
        out.append((ph.get("jp", ""), "nanami"))
    interview = unit.get("kind") == "interview"
    for c in unit.get("curveballs", []):
        out.append((c.get("npc_jp", ""), "nanami"))
        out.append((str(c.get("reply_jp", "")).split("／")[0], "keita" if interview else "nanami"))
    for v in unit.get("vocab", []):
        out.append(((v.get("reading") or v.get("word") or "").split("／")[0], "nanami"))
    return [(t, v) for t, v in out if jc._clean_tts_text(t)]


def main() -> int:
    only = set(sys.argv[1:])
    units = jc.load_interview_units() + [s for s in jc.load_scenes() if s.get("status") != "archived"]
    if only:
        units = [u for u in units if u.get("id") in only]
    todo = []
    for u in units:
        todo += items_for(u)
    seen, made, cached, failed = set(), 0, 0, 0
    for text, voice in todo:
        key = (voice, jc._clean_tts_text(text))
        if key in seen:
            continue
        seen.add(key)
        before = len(list(jc.TTS_CACHE_DIR.glob("*.mp3"))) if jc.TTS_CACHE_DIR.exists() else 0
        b64 = jc.synthesize_cloud_tts("", text, voice, RATE)
        after = len(list(jc.TTS_CACHE_DIR.glob("*.mp3"))) if jc.TTS_CACHE_DIR.exists() else 0
        if not b64:
            failed += 1
            print("✗ 失败：", text[:30])
            time.sleep(1)
        elif after > before:
            made += 1
        else:
            cached += 1
    print(f"单元/场景 {len(units)} 个，句子 {len(seen)} 句：新生成 {made}，已有缓存 {cached}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
