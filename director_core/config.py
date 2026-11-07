"""Legacy defaults; caller must copy before changing."""
from typing import Any
DEFAULT_CONFIG: dict[str, Any] = {
    # ---- 路径与网络 ----
    "cs2_exe": "",
    "gsi_port": 3000,
    "window_title": "Counter-Strike 2",
    # hlae_* 是早期误命名的兼容键；当前实际主适配目标是 MulNX，不改旧配置字段。
    # ---- MulNX 快捷键（必须由用户在 GUI 录入，留空则禁止启动，避免误按）----
    # 出生点分 CT/T：按上一回合胜方选择。
    "hlae_spawn_ct_key": "",
    "hlae_spawn_t_key": "",
    "spawn_cam_default_side": "CT",  # 第一回合/无上回合记录时用哪边
    # 包点分 A/B：按炸弹安放位置自动选择对应快捷键
    "hlae_bomb_a_key": "",
    "hlae_bomb_b_key": "",
    # 兼容旧配置：旧版 hlae_round_key / hlae_bomb_key 会自动迁移（见 load_config）
    "hlae_round_key": "",
    "hlae_bomb_key": "",
    # 触发方式：single=按一次；double=连按两次（《星火杯导播快捷键指南》的
    # alt+按键 交互中，MulNX 宏的 comboClick 为 2）。
    "hlae_spawn_ct_mode": "double",
    "hlae_spawn_t_mode": "double",
    "hlae_round_mode": "double",  # 旧版兼容
    "hlae_bomb_a_mode": "double",
    "hlae_bomb_b_mode": "double",
    "hlae_bomb_mode": "double",  # 旧版兼容
    # 时序：按住 Alt → 80ms → N 30ms → 80ms 后第二次 N 30ms
    # → 保持 Alt 约 300ms → 松开。MulNX 需要 >200ms 才会消费连击缓冲。
    "hotkey_double_interval": 0.08,  # 两次主键按下的间隔（从第一次按下算）
    "hotkey_press_hold": 0.03,       # 主键按住时长
    "hotkey_modifier_lead": 0.08,    # Alt 按下后先等多久
    "hotkey_modifier_release_delay": 0.05,  # 兼容项：最后一次主键后等多久松开 Alt
    # MulNX 的输入系统是 GetAsyncKeyState 轮询 + 200ms 连击缓冲：
    # 它要等主键按下 200ms 后才把 ComboClick 放进 BufferComboClick，
    # 并在那一刻检查 Alt 必须仍按住。因此 Alt 必须在最后一次主键后
    # 至少保持 200ms；默认 0.30s 留出轮询/帧回调余量。
    "hotkey_modifier_hold_after": 0.30,
    # ---- DeepSeek 仲裁（可选）----
    "ai_enabled": False,
    "arbitration_mode": "off",
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-flash",
    "ai_timeout": 2.5,          # 单次请求超时（秒），超时立即降级到规则引擎
    "ai_min_interval": 3.0,     # 两次 API 调用最小间隔，控制成本
    "ai_max_per_minute": 12,
    "ai_result_ttl": 2.5,       # AI 建议的有效期（秒）
    "ai_ambiguity_margin": 0.08,  # 规则前两名分差小于该值才请求 AI
    # ---- 离线 demo 模型（静态权重，不在线更新）----
    "demo_model_enabled": True,
    "demo_model_min_samples": 20,
    "demo_model_blend": 0.25,
    # ---- 专业导播：OBS / 事件动作 ----
    "obs_host": "127.0.0.1",
    "obs_port": 4455,
    "obs_password": "",
    "event_actions_enabled": True,
    "event_rules": [],
    "pause_hotkey": "ctrl+alt+p",
    "narrative_enabled": True,
    "obs_narrative_enabled": False,
    "obs_narrative_source": "",
    # ---- 决策日志 + 轻量模型 ----
    "decision_log_enabled": True,
    "decision_log_interval": 1.0,
    "decision_label_window": 3.0,
    "decision_model_enabled": True,
    "decision_model_margin": 0.08,
    "decision_model_min_factor": 0.9,
    "decision_model_prob_margin": 0.05,  # 模型推荐目标需领先当前目标的概率
    "decision_model_min_prob": 0.55,     # 模型推荐目标的最低概率
    "decision_calibration_enabled": True,
    "decision_calibration_min_samples": 500,
    "decision_calibration_after_match_only": True,
    "decision_model_calibrated_min_prob": 0.16,
    "decision_model_calibrated_prob_margin": 0.02,
    # ---- 专业导播 Phase 1：镜头时间线 / 专业指标 / 回合故事 ----
    "shot_timeline_enabled": True,
    "shot_timeline_dir": "",       # 空 = %APPDATA%\CS2AIDirector
    "metric_kill_lookback": 2.5,   # 击杀前多少秒内镜头在击杀者
    "metric_prefight_lookback": 2.5,  # 首次伤害前多少秒内镜头在当事人
    "metric_engagement_gap": 3.0,  # 多久没有伤害算“新的一段交火”
    "metric_min_live_seconds": 5.0,  # live 时间少于此值不计算 cut_rate
    "story_opening_seconds": 12.0,  # 开局阶段时长
    "story_engage_recent_seconds": 3.0,  # 最近伤害多久内算“交火中”
    "economy_full_avg": 3500.0,    # 金钱兜底：全起平均余额
    "economy_force_avg": 2000.0,   # 金钱兜底：强起平均余额
    "economy_equip_full": 1.5,     # 装备判定：全起平均档位
    "economy_equip_force": 0.8,    # 装备判定：强起平均档位
    "match_point_score": 12,       # CS2 MR12：先到 12 分拿到赛点
    # ---- 视角切换 ----
    "manual_pause_seconds": 10.0,
    "loop_hz": 20,
    "engage_range": 1500.0,     # 基础交火距离（CS 单位，约 28m）
    "aim_fov_deg": 70.0,        # 认为“正在瞄准”的最大偏角
    "max_vertical_diff": 300.0,  # 跨楼层超过该高度差视为不可能交火（Nuke 放宽）
    "hard_min_switch_interval": 0.2,  # 任意两次切换的硬间隔（秒）
    "switch_min_score": 0.30,   # 低于该分数宁可不切
    "switch_margin": 0.10,      # 新目标必须比当前目标高出的分数，防抖
    "emergency_score": 0.72,    # 极高威胁时允许绕过 margin
    "min_dwell_seconds": 1.0,   # 两次切换的最小间隔
    "min_shot_duration": 2.0,   # 专业导播最短镜头停留（与 min_dwell 取较大值）
    "max_cuts_per_10s": 3,      # 10 秒窗口内最多切换次数；<=0 关闭
    "repeat_guard_seconds": 4.0,  # 同一 slot 的重复按键保护
    "fight_lock_enabled": True,  # 当前 POV 正在交火时不切走
    "fight_lock_aim": 0.60,      # 双方都瞄准到该阈值也视为交火
    # ---- 开局控图：优先把镜头给手持/投掷道具的选手 ----
    "utility_cam_enabled": True,
    "utility_cam_pre_buffer": 0.35,   # 手持道具后确认并预切的缓冲（运行时夹在 0.25-0.5s）
    "utility_cam_post_hold": 0.35,    # 投掷后继续保持镜头的缓冲（运行时夹在 0.25-0.5s）
    "utility_cam_fight_lookback": 2.5,  # 多久内出现伤害/高威胁候选视为“场上偏热”
    "utility_cam_min_interval": 3.0,  # 两次道具镜头的间隔
    "utility_cam_max_per_round": 4,   # 每回合最多道具镜头次数；<=0 不限制
    "utility_cam_types": [            # 默认不含诱饵弹
        "weapon_smokegrenade", "weapon_flashbang", "weapon_molotov",
        "weapon_incgrenade", "weapon_hegrenade",
    ],
    "gsi_stale_seconds": 3.0,   # 超过该时间没有 GSI 数据视为断流
    "self_press_guard": 0.30,   # 模拟按键后多久内忽略同键，防自捕获
    # ---- 运镜 ----
    "bomb_cam_countdown": 8.0,  # 炸弹剩余多少秒触发包点运镜
    # pov_first：有玩家存活时优先玩家 POV，跳过包点运镜；
    # always：不论是否有人存活都触发（旧行为）。
    "bomb_cam_policy": "pov_first",
    "bomb_cam_grace": 1.5,      # 触发包点运镜后暂停自动切换的秒数（0=不暂停）
    "trigger_retry_seconds": 1.0,
    # 包点判定参考点（世界坐标，x/y）。由雷达包点区域反推，并用相机包
    # 中带 A/B 标签的路径点交叉验证；用户可在 config.json 覆盖/补充地图。
    "bomb_site_refs": {
        "de_mirage":  {"A": [-461, -2194], "B": [-2080, 254]},
        "de_inferno": {"A": [2055, 440],  "B": [389, 2792]},
        "de_dust2":   {"A": [1132, 2499], "B": [-1545, 2688]},
        "de_anubis":  {"A": [1350, 2200], "B": [-1079, 667]},
        "de_ancient": {"A": [-1430, 846], "B": [774, 75]},
        "de_cache":   {"A": [-286, 1806], "B": [-47, -1247]},
        # Nuke 的 A/B 上下重叠，用 3D 距离（B 在负层）
        "de_nuke":    {"A": [326, -319, -30], "B": [352, -572, -731]},
    },
    # ---- 其它 ----
    "auto_focus": True,         # 按键前自动把 CS2 拉到前台
    "dry_run": False,           # 演练模式：只算不按
}
