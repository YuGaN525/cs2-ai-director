# CS2 AI 专业导播 — 目标、开发日志与关键词

最后更新：2026-10-21  
当前主程序：**v1.9.0**  
当前训练器：**v1.1.0**  
现有通用交接：`HANDOFF.md`

---

## 1. 新目标

目标不是“把击杀预测 AUC 再提高一点”，而是：

> **让 AI 像一个真正的专业 CS2 导播：有预判、有叙事、有镜头节奏、有重点，而不是只会追着交火切 POV。**

专业导播和当前系统的区别：

| 维度 | 当前系统 | 专业导播 |
|---|---|---|
| 决策方式 | 每个 tick 评分，谁分高切谁 | 先形成“这一回合的故事”，再安排镜头 |
| 时间尺度 | 反应式，看到交火才切 | 预判 1–3 秒后的交火 |
| 镜头类型 | 基本只有 POV + 出生点/包点 | POV、俯视、包点、回放、自由镜头、选手特写 |
| 节奏 | 1 秒 dwell，可能频繁切 | 镜头有最短停留、切点、铺垫和收尾 |
| 叙事 | 只看当前血量/击杀 | 残局、下包、强起、明星选手、比分压力 |
| 回放 | 事件动作里独立触发 | 回放是导播叙事的一部分 |
| 评估 | 击杀覆盖、AUC | 击杀覆盖 + 交火前铺垫 + 残局覆盖 + 观众体验 |
| 人工 | 手动接管暂停 AI | 人工接管作为偏好数据，持续改进 |

一句话：

> **当前是“POV 自动切换器”；目标是“回合导演系统”。**

---

## 2. 专业导播的核心原则

### 2.1 预判，而不是追击杀

- 在交火发生前 1–3 秒切到最可能交火的选手；
- 不是等击杀发生后再切；
- 对枪前要有“铺垫镜头”，让观众看到站位、道具、推进。

### 2.2 叙事，而不是单点

一回合至少要有故事线：

```text
开局站位/道具 → 地图控制 → 首次交火 → 残局/下包 → 回放/收尾
```

导播要知道：

- 哪边经济局/强起；
- 谁手感热；
- 哪边比分压力大；
- 是否下包、是否残局、是否拆包；
- 哪些选手是明星/主线人物。

### 2.3 镜头语言

专业导播不会一直用第一视角：

- **POV**：对枪、残局、个人操作；
- **俯视/全景**：开局、道具、地图控制、战术展开；
- **包点镜头**：下包、拆包、守包；
- **回放**：击杀后回看关键枪；
- **特写/环境**：情绪、选手、场馆（如素材可用）。

### 2.4 节奏

- 每个镜头有最短停留时间（建议 2–4 秒）；
- 切点选在事件边界，而不是随机 tick；
- 连续切人要有“切换成本”，不能为了追 0.02 分差乱切；
- 高强度交火时减少切换，让观众看清；
- 回合结束/回放时给足时间。

### 2.5 人工接管是数据

人工接管不是“AI 失败”，而是最高质量的偏好标签：

- 人工从谁切到谁；
- 在什么局面切；
- 切完后观众看到了什么；
- 下次模型应该学习这个选择。

---

## 3. 目标架构

```text
GSI / demo
   │
   ▼
感知层 Perception
  - 回合阶段、比分、经济、炸弹、存活
  - 位置/朝向/血量/武器/道具
  - 交火/伤害/击杀事件
   │
   ▼
故事状态 StoryState
  - 当前回合剧本：开局/控制/交火/残局/下包/回放
  - 双方经济、强起、赛点、连胜
  - 选手状态：手感、存活、角色
   │
   ▼
镜头候选 ShotCandidates
  - POV(slot)
  - 包点/炸弹
  - 俯视/全景
  - 回放
  - 出生点
   │
   ▼
镜头效用 Utility
  - 交火概率
  - 叙事重要性
  - 新鲜度/避免重复
  - 切换成本
  - 镜头类型多样性
   │
   ▼
调度器 Scheduler
  - 最短停留
  - 最大切率
  - 事件优先级
  - 人工接管
  - 安全回退
   │
   ▼
执行层 Executor
  - 0–9 观察者键
  - HLAE 宏
  - OBS 场景/媒体
  - FFmpeg 回放
   │
   ▼
反馈/学习 Feedback
  - 决策日志
  - 人工接管
  - 好/坏镜头标记
  - 离线训练
```

核心变化：

- 不再“每个 tick 选一个最高分选手”，而是**生成多个镜头候选，做带约束的调度**；
- “不切”本身也是一个候选；
- 镜头有持续时间、切换成本、叙事权重；
- 模型输出的是**镜头效用**，不只是击杀概率。

---

## 4. 镜头候选设计

建议先定义 5 类镜头：

```text
POV(target_slot)
BOMB(site)
OVERVIEW(zone)
REPLAY(round)
SPAWN(side)
```

每个候选包含：

```text
type
target
priority
expected_duration
transition_cost
story_weight
created_at
expires_at
```

示例：

```text
POV(明星选手) 在残局 1v2：story_weight 高，duration 4–6s
BOMB(A) 下包后：priority 高，duration 3–5s
OVERVIEW(进攻方 execute)：开局/道具阶段优先
REPLAY：回合结束后，不与 live POV 竞争
```

---

## 5. 镜头效用函数（建议）

```text
utility(shot) =
    w1 * engagement_probability
  + w2 * narrative_importance
  + w3 * context_relevance
  + w4 * novelty
  + w5 * shot_type_priority
  - w6 * transition_cost
  - w7 * repetition_penalty
```

约束：

```text
min_shot_duration = 2.0–4.0s
max_cuts_per_10s = 2–3
hard_min_switch_interval = 0.2s（保留）
manual_override = 10s（保留）
```

事件优先级：

```text
下包/拆包 > 残局 1vX > 正在交火 > 即将交火 > 地图控制 > 默认 POV
```

但高优先级也要受“最短停留”和“切换成本”约束，避免观众眩晕。

---

## 6. 专业导播需要的上下文特征

在现有 8 个候选特征之外，增加：

### 回合

```text
alive_ct, alive_t, alive_diff
bomb_planted, bomb_time_left
round_time_left
score_diff, match_point
economy_ct, economy_t, eco/force/full
```

### 选手

```text
target_is_star / target_storyline
target_recent_kills, target_recent_deaths
target_recent_damage
target_awp / target_weapon
target_is_defusing
target_is_planting
```

### 空间

```text
distance_to_bomb
distance_to_teammates
zone / floor
vertical_diff
enemy_aim_on_target
target_aim_on_enemy
line_of_sight（如可获得）
```

### 镜头

```text
current_shot_type
current_shot_age
last_shot_type
last_cut_time
cuts_last_10s
manual_override_active
```

这些特征要同时进入：

- 规则评分；
- 决策日志；
- 训练 CSV；
- 模型推理。

---

## 7. 评估指标（专业导播版）

不要只看 AUC。

| 指标 | 定义 | 目标 |
|---|---|---|
| `kill_coverage` | 击杀前 2.5s 镜头在击杀者 | 越高越好 |
| `pre_fight_coverage` | 首次伤害前镜头在当事人 | 越高越好 |
| `clutch_coverage` | 1vX 残局镜头覆盖 | 越高越好 |
| `post_plant_coverage` | 下包后镜头覆盖包点/拆包 | 越高越好 |
| `corpse_time` | 停在尸体/无效画面 | 越低越好 |
| `bad_switch_rate` | 切过去 3s 无伤害/无击杀/阵亡 | 越低越好 |
| `cut_rate` | 每分钟切换次数 | 有上限 |
| `shot_duration` | 平均镜头停留 | 2–5s |
| `shot_variety` | 镜头类型多样性 | 不要全是 POV |
| `manual_takeover` | 人工接管次数 | 越低越好 |
| `key_to_observer_ms` | 按键到 observer_slot 变化 | < 300ms |
| `A/B preference` | 人工盲测偏好 | > 50% |

---

## 8. 开发阶段

### Phase 0：可观测性（优先）

- 记录完整 **shot timeline**：
  - 每个镜头类型/目标/开始/结束/原因；
  - 切换前后画面；
  - 人工接管；
  - 实际 observer_slot。
- 统计上面的指标。
- 实机联调 OBS/FFmpeg，确保回放不会打断叙事。

**没有 timeline，就无法判断“像不像专业导播”。**

### Phase 1：回合故事状态

- 识别回合阶段：开局、地图控制、交火、下包、残局、回放；
- 经济判断：eco/force/full；
- 残局识别：1v1/1v2/2v1；
- 下包/拆包/保枪；
- 比分压力/赛点。

### Phase 2：镜头候选与调度器

- 从“选一个人”升级为“选一个镜头”；
- 加入 `OVERVIEW/BOMB/REPLAY` 候选；
- 最短镜头时长、最大切率、切换成本；
- “不切”候选；
- 事件优先级。

### Phase 3：多机位与 HLAE 扩展

- 扩展 HLAE 宏库：
  - 俯视/全景；
  - 包点；
  - 回放；
  - 自由镜头；
  - 选手特写；
- GUI 允许配置更多快捷键；
- 镜头类型与 HLAE 宏绑定。

### Phase 4：回放叙事

- 回合结束自动回放关键击杀；
- 回放时长/片段选择；
- 回放后回到 live；
- 回放与 OBS 场景/媒体源联动；
- 不打断关键 live 回合。

### Phase 5：学习专业导播

- 人工接管作为偏好样本；
- 记录“好/坏镜头”；
- 学习镜头效用权重；
- 序列模型/模仿学习（仅当数据足够）；
- 离线评估 + shadow mode + 人工盲测。

---

## 9. 训练体系怎么变

当前训练目标是“目标 3 秒内是否造成伤害/击杀”。

专业导播需要更多标签：

```text
shot_outcome: 击杀/伤害/无交火/阵亡
shot_quality: 人工好/坏
narrative_value: 残局/下包/赛点/明星选手
transition_quality: 切点是否自然
```

训练目标从二分类升级为：

1. **候选排序**：哪个镜头最值得看；
2. **效用回归**：这个镜头的综合价值；
3. **偏好学习**：人工在 A/B 中更喜欢哪个；
4. **序列决策**：什么时候切、什么时候不切。

数据来源：

- 线上 decisions.csv；
- demo 决策模拟器；
- 人工接管；
- 专业比赛录像的人工标注（如允许）；
- OBS 回放结果。

不要一开始就做端到端深度学习；先把**镜头 timeline 和人工偏好**积累起来。

---

## 10. 当前系统与目标的差距

已具备：

- GSI 读取、POV 评分、HLAE 出生点/包点；
- 观察对象阵亡回切；
- 独立日志、事件动作、OBS、FFmpeg 回放；
- 决策 CSV、RF/ExtraTrees、自动重载；
- 独立训练器、demo 导入。

缺少：

- 镜头 timeline / 镜头类型；
- 回合故事状态；
- 镜头调度器；
- 多机位 HLAE 宏库；
- 人工偏好标签；
- 专业导播评估体系；
- 真实 OBS/FFmpeg 联调。

---

## 11. 新关键词

### 中文

```text
专业导播
镜头语言
镜头调度
镜头候选
镜头效用
镜头预算
最短停留
切换成本
切点
叙事连续性
回合故事线
残局
下包后
经济局
强起
赛点
明星选手
多机位
俯视镜头
包点镜头
回放叙事
人工接管
偏好学习
盲测
导演意图
```

### 英文

```text
professional esports director
shot planning
shot candidate
shot utility
shot budget
minimum shot duration
transition cost
cut point
narrative continuity
round story graph
clutch
post-plant
economy context
force buy
match point
star player
multi-camera
overview shot
bomb site shot
replay narrative
human-in-the-loop
preference learning
blind A/B test
director intent
```

### 代码/文件关键词

```text
ShotCandidate
ShotScheduler
StoryState
RoundPhase
ShotTimeline
shot_utility
transition_cost
narrative_weight
min_shot_duration
max_cuts_per_10s
manual_override
```

---

## 12. 新对话开场提示（可直接复制）

```text
继续开发 CS2 AI 导播，新目标是“像真正的专业导播”，不是只提高击杀预测 AUC。

请先阅读：
- PRO_DIRECTOR_HANDOFF.md
- HANDOFF.md
- README.md

当前系统：
- 主程序 v1.9.0，训练器 v1.1.0；
- 已具备 GSI/POV/HLAE/事件动作/OBS/FFmpeg/决策日志/RF-ExtraTrees；
- 当前是反应式 POV 切换器，没有镜头 timeline、故事状态、镜头调度器；
- OBS/FFmpeg 尚未实机联调；
- 训练样本不足 1000 条。

硬性约束：
- 只读 GSI，不读内存/不注入/不发控制台命令；
- 只模拟 0-9 观察者键和 HLAE 快捷键；
- 不用深度学习作为第一步；
- 训练器与主程序共享评分核心，修改时两个仓库要同步。

第一阶段请只做：
1. ShotTimeline 记录（镜头类型/目标/开始/结束/原因/人工接管）；
2. 专业导播指标计算（kill_coverage、pre_fight_coverage、corpse_time、cut_rate、shot_duration、manual_takeover）；
3. 回合故事状态（阶段/经济/残局/下包/比分）；
4. 不要先改模型，不要先做深度学习。

先检查现有代码，给出最小改动方案，再实施。
```

---

## 13. 一句话总结

> **专业导播 = 预判 + 叙事 + 镜头语言 + 节奏 + 人工反馈。**

当前项目已经有“眼睛”（GSI）、“手”（HLAE/OBS）和“反应”（POV 评分），下一步要补的是“导演的大脑”：**故事状态、镜头候选、调度器和评估体系**。
