# 训练器迁入前说明（历史参考）

以下为 5dde4dd 的原文；独立 exe/双仓同步已由主 README 替代。原文模型对比有效性须按 AID-02 重新验证，不作为本轮效果承诺。

# Yuzi_CS2_AIDecisionTrainer

独立训练器，供 [CS2 AI Director](https://github.com/YuGaN525/cs2-ai-director) 读取训练成果。

## 两种训练数据

1. **导播在线决策日志** `decisions.csv`
   - 由主程序在 `%APPDATA%\CS2AIDirector\decisions.csv` 生成；
   - 每行含目标 slot、规则分和 8 个候选特征；
   - 3 秒后按 `round_totaldmg` / `round_kills` 回填 label。

2. **已有的 CS2 demo `.dem`**
   - 选择文件或文件夹；
   - 回看击杀前 160 tick（64tick ≈ 2.5 秒）；
   - 击杀者 label=1，同刻其他候选 label=0；
   - 生成 `demo_decisions.csv` 后走同一个随机森林流程。

## 使用

### GUI

运行 `Yuzi_CS2_AIDecisionTrainer.exe`：

- 选择 `decisions.csv` 或 `.dem` 文件/文件夹；
- 设置最少样本（默认 1000）、树数量、最大深度、验证集比例；
- 点“开始训练”或“解析 demo 并训练”；
- 输出：

```text
%APPDATA%\CS2AIDirector\decision_model.pkl
%APPDATA%\CS2AIDirector\decision_model.json.gz
%APPDATA%\CS2AIDirector\decision_model_metrics.json
```

主程序每 5 秒检测模型文件更新，自动重新加载，无需重启。

### 命令行

```bat
Yuzi_CS2_AIDecisionTrainer.exe --cli --csv decisions.csv --min-samples 1000

Yuzi_CS2_AIDecisionTrainer.exe --cli --ai --csv decisions.csv

Yuzi_CS2_AIDecisionTrainer.exe --cli --demos "D:\demos" --min-samples 1000
```

## AI 辅助训练（v1.2.0）

在数据量与标签不变的前提下，用“AI 顾问 + 本地验证”调整超参数与样本权重：

1. 生成数据画像：样本量、正负比、模糊样本、分组、地图分布、特征单变量 AUC（不读取 SteamID/昵称）；
2. 启用 DeepSeek 时，把聚合画像发给 `deepseek-chat`，返回诊断、风险与搜索空间建议；
3. 本地先用默认参数训练基线，再按 AI 建议（或本地扩展搜索）训练候选；
4. 候选必须在**同一验证集**上 AUC 提升 ≥0.2pp 或 top-1 提升 ≥1pp，且另一项不明显退步（AUC -0.5pp / top-1 -2pp）、JSON ≤20MB 且 ≤基线 1.5 倍，才会被采用；否则保留基线；
5. 写出 `ai_training_report.json`，记录两组指标、建议与采用原因。

使用方式：

- GUI：勾选“启用 DeepSeek 顾问”，点“AI 辅助训练（CSV）”；
- 命令行：`--cli --ai --csv decisions.csv`；`--no-ai` 可强制只做本地扩展搜索。

Key 来源与隐私：

- 优先读环境变量 `DEEPSEEK_API_KEY`，否则读导播的 `%APPDATA%\CS2AIDirector\config.json`；
- 只发送聚合统计与指标，不发送 SteamID、昵称或逐行数据；
- 无 Key、超时、HTTP 错误或 JSON 非法时自动回退本地搜索，训练不中断；
- AI 只能建议超参数、样本权重与切分方式，不能改特征/标签/模型格式，也不引入深度学习或新依赖。

输出：

```text
%APPDATA%\CS2AIDirector\ai_training_report.json
```

报告包含 `diagnosis / data_risks / search_space / weight_boost / split_mode`、基线/候选指标与 `decision`。启用 AI 时训练时间约为普通训练的两倍（基线与候选各训练一次）。

## 模型选择

调研过公开 CS2 模型后的结论：

- [awpy](https://github.com/pnxenopoulos/awpy)（MIT）是 demo 解析库，不含“下一击杀”模型；
- [cs2wp](https://github.com/michaeltgit/cs2wp) 用 LightGBM 预测回合胜率，但仓库没有 LICENSE，不能直接复用其权重；
- [CS2_Win_Prediction](https://github.com/Hentoshp/CS2_Win_Prediction)（MIT）是团队胜负 CNN，不是镜头目标模型；
- 没有找到许可证清晰、特征与 GSI 对齐的“下一击杀/镜头目标”预训练模型。

因此保留可审计的开源 scikit-learn 方案，并升级为：

- **RandomForest vs ExtraTrees** 两类树模型；
- 3 折交叉验证 + 默认 8 组随机超参数搜索（AI 建议 4–24 组，实际按数据量限流）；
- 按交叉验证 AUC 选择最终模型；
- 仍导出纯 JSON 森林，导播端无需 sklearn。

- 输出验证集 AUC、混淆矩阵、分类报告、特征重要性；
- 特征：`aim_self, aim_enemy, win, enemy_low_hp, self_low_hp, proximity, enemy_damaged, hp_advantage`；
- 主程序只在规则前两名分差 ≤ `decision_model_margin` 时用模型仲裁；
- 模型推荐目标的规则分不得低于当前第一名的 `decision_model_min_factor`；
- 主程序通过纯 JSON 森林加载，不需要安装 scikit-learn。

## 目录说明

训练器的 demo 解析复用主程序的评分/快照代码，因此包含以下共享文件：

```text
cs2_ai_director.py
director_story.py
director_pro.py
decision_model.py
train_from_demos.py
```

它们与主仓库同源；修改评分特征时应同时更新两个仓库，避免训练/推理特征不一致。

当前共享核心同步版本：**v1.10.18（2026-10-24）**，已同步
`cs2_ai_director.py`、`director_pro.py` 与 `test_cs2_ai_director.py`（129 项测试）。
v1.10.18 只新增导播运行时的开局/控图道具镜头，不改变 `DECISION_FEATURES`、评分与模型格式，
训练器不需要重训或重建 exe。

## 专业导播指标（v1.10.7）

主仓库 v1.10 新增、v1.10.7 同步 `director_story.py`：镜头时间线、回合故事和六项专业指标
（`kill_coverage / pre_fight_coverage / corpse_time / cut_rate / shot_duration / manual_takeover`）。
本训练器已同步该模块，以保证共享核心副本完整、demo 解析不会因缺文件而 ImportError。

但**这些指标暂时不进入训练特征**：

- 它们是回合级/镜头级评估指标，不是 `DECISION_FEATURES` 里的 8 个候选特征；
- 指标由导播自己的镜头选择产生，把它当特征会造成“镜头影响指标、指标再影响镜头”的循环；
- 线上推理时也拿不到未来回合的完整指标，强行加入会造成训练/推理时点不一致；
- `decision_model.json.gz` 仍是 8 特征纯 JSON 森林，不需要重训或换格式。

后续 Phase 2 做镜头候选/偏好学习时，应单独建镜头级数据集与模型，不要改这个 8 特征击杀模型。

## 数据来源：不要混淆

- `.dem` 导入：训练器自己按击杀窗口构造候选，标签是“窗口内造成伤害/击杀”，**不是导播当时的实际决策**；
- `decisions.csv`：导播实际运行时写出的目标、规则分、动作和 3 秒标签，**才是“学导播实际选择”的数据**。

如果目标是复现/优化这五张图的导播行为，应使用 `decisions.csv` 训练；
`.dem` 只适合扩充候选级样本。

## 训练策略（v1.10.7）

- 训练/验证按 `group` 切分：在线 CSV 用 `map|round`，demo 用击杀事件；
- `is_ambiguous=1`（规则前两名接近）样本权重更高，贴近模型实际被调用的场景；
- `switch` 样本略加权；
- demo 标签改为“回看窗口内造成过伤害/击杀”，与 `decisions.csv` 对齐；
- demo 用 `player_hurt` 重算 `damage_recent`，避免 `enemy_damaged` 恒为 0；
- demo 同时写入 `rule_gap/is_ambiguous`，让模糊样本加权真正生效；
- 评估同时输出 AUC 和 top-1（候选组内概率最高者是否命中）；
- 指标接近时优先选节点更少的模型：AUC 差 ≤0.005、top-1 差 ≤1pp 时优先 RandomForest；
- 完成弹窗显示模型 JSON 大小，超过 20MB 给出性能警告。
- 主程序仲裁要求概率 ≥ `decision_model_min_prob` 且领先当前目标
  `decision_model_prob_margin`，避免弱概率切换。

## 在线校准（v1.1.4）

训练器新增“校准线上模型”按钮，用 `%APPDATA%\CS2AIDirector\decisions.csv`
拟合 Platt 校准：

- 按 `group`（回合）切分训练/验证；
- 只有 Brier 和 log-loss 都改善、且 `a > 0.05` 才写
  `decision_calibration.json`；
- 保存 `model_mtime`，模型重训后旧校准自动失效；
- 导播比赛结束且标注样本 ≥500 时自动执行一次校准；
- 校准后使用 `decision_model_calibrated_min_prob` / `_margin`。

CLI：

```bat
Yuzi_CS2_AIDecisionTrainer.exe --cli --calibrate --csv decisions.csv --model decision_model.pkl --out decision_calibration.json --min-samples 500
```

## 构建

```bat
python -m pip install -r requirements-train.txt
powershell -ExecutionPolicy Bypass -File .\build_trainer.ps1
```

产物：`dist\Yuzi_CS2_AIDecisionTrainer.exe`。

训练器内置 scikit-learn、joblib、demoparser2、pandas，体积约 179 MB；未签名，SmartScreen 可能提示。

## 测试

```bat
python -m unittest
```

当前 140 项通过：129 项共享核心测试 + 11 项 AI 辅助训练测试（画像隐私、建议校验、守卫、搜索预算、无 Key 回退、报告落盘）。

## 边界

- 训练需要至少 1000 条已标注样本（GUI 可调低，但不建议）；
- demo 的 tickrate 不是 64 时，调整“回看 tick”（128tick 用 320）；
- demo 仍是“击杀前 2.5 秒”采样，不等同于逐帧完整模拟；但标签/特征已尽量对齐在线；
- OBS/回放/改名不属于训练器功能。
