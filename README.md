# 日语口语私教台（Japanese Speaking & Interview Coach）

一个在本机常驻运行的日语口语与面试练习 Web 应用：浏览器录音或打字作答，Gemini 只负责**评估用户的输入**；题目、模板、场景等内容事先写好存在 `data/` 里。

## 1. 项目架构概览

| 页面 | 作用 |
| :--- | :--- |
| `/interview` 面试单元（2026-11-15 起的每日主线） | 每个 Day 一个单元：示范对话 → 单词与表达（口头版 / 书面版对照）→ 表达自测 → 实战模拟（Gemini 扮面试官追问）。实战模拟的总结里点「✅ 打卡」才进下一天，没打卡就继续这一课；打过卡的单元随时复习。单元 `data/06_日本語口語/面试单元/day_NN.json` 由 Claude 在对话里写，打卡记录 `_checkins_<电脑名>.jsonl`（只追加，两台电脑合计）。每日邮件只发提醒和链接 |
| `/` 主对练台 | 自由对练：场景 A～D 的开场题和「下一题」从题库抽（通用题模板 + 背景题，0 Gemini），录音或文字提交后由 Gemini 批改；批改只给一个「照着说」版本 |
| `/bank` 面试题库三阶闯关 | 20 道通用面试题加公司版模板，分三阶练（看稿 → 填空 → 脱稿）；可「🎲 加公司背景再问」，从背景题池里抽一个带 JD/公司背景的问法 |
| `/scenes` 生活场景演练 | 药店、区役所、牙科、快递等日常场景：先看示范对话，再做表达自测，最后和 AI 角色扮演；「随机应变」模式会插入意外情况（自动、总是关、总是开） |
| `/progress` 进度 | 五维评分走势、错题与需要复练的表达追踪 |

**Local-First 架构**：Web 服务只读写本机 SSD 上的 `data/`，从不直接访问 OneDrive。这样可以避开 macOS OneDrive 客户端崩溃，或 `dataless` 占位符导致的读取死锁（`Errno 11` / `Errno 60`）。和 OneDrive 之间的同步交给后台的 `sync_onedrive.py`。

```
浏览器 ──HTTP──▶ jp_coach.py ──读写──▶ data/（本机 SSD）
                    │                     ▲
                    │ 写入后异步触发       │ 双向增量同步（另有 5 分钟定时巡检）
                    └──────▶ sync_onedrive.py ◀──▶ OneDrive 资料库
```

## 2. 核心脚本

- **`jp_coach.py`**：Web 服务加教练引擎，只用 Python 标准库，没有第三方依赖。
  - 基于 `ThreadingHTTPServer` 的多线程服务。
  - 调用 Gemini 的多模态接口批改录音和文字（音频直接上传，不先转写）。
  - 用 edge-tts 生成示范朗读音频，结果缓存在 `data/tts_cache/`。
  - 提供 `/static/` 图标和 `manifest.webmanifest`，手机浏览器可以「添加到主屏幕」。
- **`sync_onedrive.py`**：本机 `data/` 与 OneDrive 资料库之间的异步双向同步器。
  - 用 `fcntl` 文件锁保证同一时间只跑一个实例。
  - 原子写入：先写临时文件，再 rename。
  - 文件按 mtime 比较新旧；日志和台账这类文件按「只追加」合并。
  - 遇到 OneDrive 未就绪的文件就跳过，下次巡检再补。
  - 代码、本 README 和 `.gitignore` 也会双向同步到 OneDrive 的 `05_ツール/speaking_coach/`。
- **`check_updates.py`**：列出网页上标注了、或资料更新了、需要 Claude 在对话里处理的事项（生活场景待 Archive、面试单元下一个 Day 没写、模板过时、新公司没分语言、上次检查后更新过的资料）；`--mark` 记下检查点。
- **`pregen_tts.py`**：把面试单元和生活场景的台词提前生成成语音，存进 `data/tts_cache/`（默认音色、0.9x 语速）；网页点播放时直接读缓存。写完新单元/场景后跑一次（可只传 id）。
- **`manage_life_scenes.py`**：管理生活场景的状态和安装。
  - 场景状态按 `active` → `marked_archive`（在网页上标记）→ `archived`（移进 `生活场景/archive/`）流转。
  - 子命令：`status`、`archive-marked`、`validate <新场景.json>`。

`data/06_日本語口語/` 下的主要数据：

- `面试题库.json`：通用模板。
- `面试题库_公司版.json`：公司版模板。
- `面试题库_背景题.json`：背景题池，以及每家公司的面试语言 `company_lang`（ja/en）。
- `生活场景/*.json`：生活场景。
- `記録/coach_sessions/*.jsonl`：练习记录，按日期和机器分文件。
- `口語訓練記録台帳.md`：训练台账。
- `积累库.md`：积累的表达。

## 3. 本机常驻运维速查

运行环境用 conda 的 `scratch` 环境（Python 3.12）。不要用 base，也不要装进其他环境。

### launchd 服务

| Label | 作用 |
| :--- | :--- |
| `<你的前缀>.jp_coach` | Web 服务（127.0.0.1:18765） |
| `<你的前缀>.jp_coach_sync` | 每 300 秒跑一次 `sync_onedrive.py` |

plist 放在 `~/Library/LaunchAgents/`，日志路径看 plist 里的 `StandardOutPath`。

```bash
# 状态（第一列是 PID，「-」表示没在跑；第二列是上次退出码）
launchctl list | grep jp_coach

# 重启 Web 服务（改完 jp_coach.py 之后）
launchctl kickstart -k gui/$(id -u)/$(launchctl list | awk '$3 ~ /\.jp_coach$/ {print $3}')

# 手动跑一次同步
~/miniconda3/envs/scratch/bin/python ~/Projects/日语口语私教台/sync_onedrive.py
```

### Tailscale HTTPS 访问

本机用 `tailscale serve` 把 `127.0.0.1:18765` 以 HTTPS 发布到 tailnet 内部。外网访问不到，只有同一 tailnet 里的设备能用；手机录音需要 HTTPS。

```bash
# 查看 serve 状态和访问地址（https://<本机名>.<tailnet>.ts.net）
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status

# 重新发布（后台常驻）
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 18765
```

### 密钥

- `GEMINI_API_KEY` 等配置只放在 `~/.credentials/google/jp_coach.env`，在仓库之外，也不进 git。
- 读取顺序：`~/.credentials/google/jp_coach.env` → 同目录 `.env`（已被 `.gitignore` 忽略）→ 系统环境变量。
- 个人信息（候选人描述、研究要点、面试官场景、卡壳积木、OneDrive 路径）放在同目录 `local_profile.json`，已被 `.gitignore` 忽略，并随 `sync_onedrive.py` 同步到 OneDrive。代码里只有通用默认值；没有这个文件也能运行。
- 可选开关：
  - `TEMPLATE_ALLOW_GEMINI=1`：恢复网页上生成模板。
  - `CTXQ_ALLOW_GEMINI=1`：背景题池为空时，允许 Gemini 现场出题。

## 4. 功能备注（2026-11-13/14）

- **背景题**（/bank）：抽题时挑用得最少的那一题，批改按背景版的问法来评，不影响通用模板和进度。题池由 Claude 在对话里根据 `02_企業別` 的资料来写，网页不调 Gemini 出题。日语题库只用 `company_lang` 为 ja 的公司；新公司默认按 ja 处理，英语面试的公司要加进 en。
- **随机应变**（/scenes）：「自动」档下，某段对话「✅ 任务完成」满 2 次后开启意外情况。按场景×段落分别计数，两台电脑的记录合计。阈值是 `scenes.html` 里的 `SURPRISE_AFTER`，选项存在 localStorage 的 `scene_surprise_mode`。
- **图标**：要换图标，直接替换 `static/` 里的文件即可，不用改代码。

## 5. 分工原则

- **Gemini 只评估用户的输入**：录音和文字的批改、角色扮演对话。
- 通用模板、公司版模板、背景题、生活场景这类内容，由 Claude 在对话里根据资料提前写好，存进 `data/`。网页上的模板按钮只会复制一句「写模板：…」，贴到对话里请 Claude 写。
- 后台数据先收集，再在对话里分析补全。
