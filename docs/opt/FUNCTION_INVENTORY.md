# OPT-01 功能入口与配置目录

本表从当前程序源码提取；按钮回调保留不等于每个外部服务均已通过真实联调。网站仓库未修改。

## 普通与开发者模式

| 功能 | 普通模式入口 | 开发者模式 | 生效与停止规则 |
| --- | --- | --- | --- |
| 自动导播、人工接管状态 | 工作台及固定控制条 | 相同 | 正式许可与模型门不变；停止后释放按键 |
| 游戏、GSI、MulNX、演练 | 游戏设置 | 相同 | 配置校验后下次启动；演练不绕过许可 |
| OBS、事件规则与叙事 | 专业导播页 | 相同 | 编辑为草稿；停止后应用；事件暂停不是暂停全部 AI |
| 登录、退出、模型检查、数据队列 | 设置中心 | 相同 | 使用原账号和同步服务；队列按原账号隔离 |
| 配置/模型备份与本地记录导出 | 设置中心 | 相同 | 显式选择本地目标；备份默认不含密钥 |
| CSV 与 Demo 训练 | 独立训练工作台 | 保留 | 同 exe 独立进程；候选不自动成为正式模型 |
| 高级训练参数、AI 辅助、校准 | 隐藏 | 训练工作台 | 关闭模式保留控件值与正在执行的任务 |
| 候选模型、镜头指标、完整日志、依赖 | 隐藏 | 专业页/日志页/依赖区域 | 可见性不参与授权判断 |

## 所有按钮及回调

| 文件与行 | 文本 | 回调 |
| --- | --- | --- |
| account_ui.py:12 | 登录网站账号 | `account.login_async` |
| account_ui.py:13 | 退出账号 | `lambda: threading.Thread(target=account.logout, daemon=True).start()` |
| cs2_ai_director.py:3563 | 从 demo 训练… | `self._train_demos` |
| cs2_ai_director.py:3604 | ▶ 启动导播 | `self._start` |
| cs2_ai_director.py:3607 | ■ 停止 | `self._stop` |
| cs2_ai_director.py:2999 | 连接 | `self._pro_connect` |
| cs2_ai_director.py:3001 | 断开 | `self._pro_disconnect` |
| cs2_ai_director.py:3024 | 添加 | `self._pro_add_rule` |
| cs2_ai_director.py:3026 | 编辑 | `self._pro_edit_rule` |
| cs2_ai_director.py:3027 | 删除 | `self._pro_delete_rule` |
| cs2_ai_director.py:3028 | 启用/禁用 | `self._pro_toggle_rule` |
| cs2_ai_director.py:3030 | 测试选中 | `self._pro_test_rule` |
| cs2_ai_director.py:3032 | 保存预设… | `self._pro_save_preset` |
| cs2_ai_director.py:3034 | 加载预设… | `self._pro_load_preset` |
| cs2_ai_director.py:3041 | 暂停开始 | `lambda: self._pro_pause(True)` |
| cs2_ai_director.py:3043 | 暂停结束 | `lambda: self._pro_pause(False)` |
| cs2_ai_director.py:3060 | 训练数据状态 | `lambda: __import__('desktop_ui').settings(self)` |
| cs2_ai_director.py:3061 | 打开训练工作台… | `self._open_trainer` |
| cs2_ai_director.py:3062 | 重新加载模型 | `self._reload_model` |
| cs2_ai_director.py:3064 | 校准线上模型 | `self._calibrate_online` |
| cs2_ai_director.py:3083 | 打开数据目录 | `self._open_timeline_dir` |
| cs2_ai_director.py:3113 | 刷新 | `self._refresh_pro_logs` |
| cs2_ai_director.py:3373 | 添加动作 | `add_action` |
| cs2_ai_director.py:3375 | 删除动作 | `del_action` |
| cs2_ai_director.py:3398 | 保存 | `save` |
| cs2_ai_director.py:3451 | 打开训练工作台 | `self._open_trainer` |
| cs2_ai_director.py:3453 | 账号、模型与程序状态 | `lambda: open_settings(self)` |
| cs2_ai_director.py:3454 | 配置游戏与运镜 | `lambda: notebook.select(settings_scroll)` |
| cs2_ai_director.py:3476 | 自动检测 | `self._auto_detect` |
| cs2_ai_director.py:3477 | 浏览… | `self._browse_exe` |
| cs2_ai_director.py:3487 | 一键安装 GSI | `self._install_gsi` |
| cs2_ai_director.py:3488 | 打开 cfg 目录 | `self._open_cfg_dir` |
| cs2_ai_director.py:3596 | 重新检查 | `self._refresh_deps` |
| cs2_ai_director.py:3597 | 安装缺失依赖 | `self._install_deps_async` |
| cs2_ai_director.py:3611 | 保存配置 | `self._save_only` |
| cs2_ai_director.py:3613 | 设置中心 | `lambda: open_settings(self)` |
| cs2_ai_director.py:3644 | 回到最新日志 | `lambda: self.txt.see('end')` |
| desktop_ui.py:292 | 应用外观与模式 | `apply` |
| desktop_ui.py:325 | 请求账号与正式模型重新检查 | `app.account.sync_requested.set` |
| desktop_ui.py:326 | 唤醒自动补传 | `lambda: app.training_data.uploader.wake_event.set()` |
| desktop_ui.py:340 | 导出当前账号本地记录 | `export_records` |
| desktop_ui.py:348 | 导出状态诊断（不含凭据） | `export` |
| desktop_ui.py:398 | 关闭 | `top.destroy` |
| desktop_ui.py:352 | 导出配置与模型备份 | `app._export_bundle` |
| desktop_ui.py:353 | 导入已有备份 | `app._import_bundle` |
| desktop_ui.py:381 | 选择安装目录与运镜包，核对快捷键 | `check_camera` |
| trainer_gui.py:202 | AI 辅助训练（CSV） | `self._start_ai` |
| trainer_gui.py:233 | 解析 demo 并训练 | `self._start_demo` |
| trainer_gui.py:241 | 用 CSV 开始训练 | `self._start` |
| trainer_gui.py:246 | 校准线上模型 | `self._calibrate` |
| trainer_gui.py:161 | 设置中心 | `lambda: open_settings(self)` |
| trainer_gui.py:170 | 浏览… | `self._browse_csv` |
| trainer_gui.py:176 | 浏览… | `self._browse_out` |
| trainer_gui.py:223 | 选文件 | `self._browse_demos` |
| trainer_gui.py:225 | 选目录 | `self._browse_demo_dir` |
| trainer_gui.py:244 | 打开输出目录 | `self._open_out` |
| trainer_gui.py:249 | 训练数据状态 | `lambda: __import__('desktop_ui').settings(self)` |
| trainer_gui.py:267 | 回到最新日志 | `lambda: self.log.see('end')` |

## CLI 参数兼容入口

| 文件与行 | 参数 |
| --- | --- |
| account_client.py:329 | `--report` |
| ai_trainer.py:699 | `--csv` |
| ai_trainer.py:700 | `--out` |
| ai_trainer.py:701 | `--metrics` |
| ai_trainer.py:702 | `--min-samples` |
| ai_trainer.py:703 | `--test-size` |
| ai_trainer.py:704 | `--trees` |
| ai_trainer.py:705 | `--max-depth` |
| ai_trainer.py:706 | `--no-ai` |
| ai_trainer.py:707 | `--timeout` |
| cloud_worker.py:121 | `--config` |
| cloud_worker.py:121 | `--once` |
| cloud_worker.py:121 | `--child` |
| cs2_ai_director.py:4455 | `--check` |
| cs2_ai_director.py:4456 | `--check-report` |
| cs2_ai_director.py:4457 | `--install-deps` |
| decision_calibration.py:279 | `--csv` |
| decision_calibration.py:280 | `--model` |
| decision_calibration.py:281 | `--out` |
| decision_calibration.py:282 | `--min-samples` |
| decision_calibration.py:283 | `--test-size` |
| demo_dataset.py:102 | `--demos` |
| demo_dataset.py:103 | `--out` |
| demo_dataset.py:105 | `--horizon-ticks` |
| demo_dataset.py:107 | `--tick-offset` |
| demo_dataset.py:108 | `--step-ticks` |
| evaluate_programme.py:41 | `input` |
| evaluate_programme.py:41 | `--out` |
| offline_runtime.py:88 | `--config` |
| offline_runtime.py:89 | `--report` |
| train_decision_model.py:465 | `--csv` |
| train_decision_model.py:466 | `--out` |
| train_decision_model.py:467 | `--metrics` |
| train_decision_model.py:468 | `--split-plan` |
| train_decision_model.py:469 | `--split-mode` |
| train_decision_model.py:470 | `--min-samples` |
| train_decision_model.py:471 | `--test-size` |
| train_decision_model.py:472 | `--trees` |
| train_decision_model.py:473 | `--max-depth` |
| train_from_demos.py:354 | `--demos` |
| train_from_demos.py:356 | `--out` |
| train_from_demos.py:358 | `--tick-offset` |
| train_from_demos.py:360 | `--epochs` |
| train_from_demos.py:361 | `--lr` |
| train_from_demos.py:362 | `--l2` |
| train_from_demos.py:363 | `--holdout` |
| trainer_gui.py:662 | `--demos` |
| trainer_gui.py:663 | `--demo-csv` |
| trainer_gui.py:664 | `--out` |
| trainer_gui.py:665 | `--metrics` |
| trainer_gui.py:666 | `--min-samples` |
| trainer_gui.py:667 | `--test-size` |
| trainer_gui.py:668 | `--trees` |
| trainer_gui.py:669 | `--max-depth` |
| trainer_gui.py:671 | `--horizon-ticks` |
| trainer_gui.py:673 | `--tick-offset` |
| trainer_gui.py:675 | `--step-ticks` |
| training_data_runtime.py:72 | `--root` |
| training_data_runtime.py:73 | `--report` |
| training_data_runtime.py:74 | `--export` |

## 需求到验证映射

| 需求 | 验证 |
| --- | --- |
| 草稿、隐藏配置、偏好合并 | test_desktop_optimization.ConfigurationTests / PreferencesTests / ExecutionLifetimeTests；ui_matrix.py |
| 停止、满队列、旧动作、100 次启停 | ActionShutdownTests / ExecutionLifetimeTests / GsiLifecycleTests |
| 写盘失败、背压、内容/顺序 | OrderedPersistenceTests；原 test_upload_pipeline.py 等上传测试 |
| 原规则、MulNX 时序、授权、正式模型 | 原 test_cs2_ai_director.py、test_account_client.py、test_model_runtime.py、test_device_identity.py |
| 训练入口与异步关闭 | test_trainer_integration.py；原训练及 Demo 回归 |
| 完整行为对比 | behavior_benchmark.py；仅模拟输入与反馈，不含真实节目输出 |
| 长时资源 | opt_soak.py；真实计时，模拟游戏/输入；真实 CS2/MulNX/OBS 单列待验 |
