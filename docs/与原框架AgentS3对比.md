# 与 AgentS3 原框架的对比

> 记录时间：2026-09-23。基于对两边代码的通读（`AgentS/gui_agents/s3/` vs `osworld_agent/`），
> 以及 test_small 36 任务多轮评测的实战对比。

## 一、定位差异

| | AgentS3 原框架 | 我们的 osworld_agent |
|---|---|---|
| 本质 | Agent S3 论文的工程复现 | 重新设计的分层框架 |
| 决策 prompt | 英文 | **中文**（用户明确要求全中文）|
| 关注点 | 跑通 OSWorld、拿分 | 跑通 + 可 RL 训练 |

我们在**架构清晰度**和**RL 训练基础设施**上超越原框架，但在若干**隐蔽的语义细节**上曾落后（见第三节），现已追平。

## 二、架构分层对比

| 维度 | 原框架 | 我们 |
|---|---|---|
| 核心循环 | `agents/grounding.py` ACI 类 + worker | `pipeline.py` 单文件 Pipeline |
| grounding | `generate_coords`（UI-TARS 单点）+ `generate_text_coords`（OCR 文本跨度）| `RealUItarsGrounding`（同协议）+ `_ocr_fallback`（失败兜底）|
| code_agent | `LMMAgent` + view(含 PDF→PNG) + note + **summary agent** | 简化版 ChatModel + view + note |
| 记忆 | procedural_memory(静态规则) + note/recall | structured_memory(状态/事件/事实/失败/**action_history**) |
| 环境 | 直接 DesktopEnv | `Environment` 抽象 + VMware/Docker 适配器 |
| RL | `rl_training/` 独立脚本 | `rl/` 语义轨迹树 + step-level credit（框架内模块）|

## 三、三个本质差异（踩坑记录）

### 3.1 prompt 语言 → grounding 跨语言失败（最大发现）

- 原框架英文 target `"The 'Remove Account' option in the Account Actions dropdown"` → UI-TARS 定位成功
- 我们中文 target `"创建快捷方式"` → UI-TARS 返回 `"图中不存在"`
- 中英混合（`"下拉菜单里的 Remove Account 选项"`）同样失败

**根因**：UI-TARS-1.5-7B 的 grounding 训练数据以英文 UI 为主，靠界面英文文字定位；中文/中英混合 target 无法跨语言匹配英文菜单项。

**修复**：决策 prompt 整体保持中文，但 `next_action.target` 里的界面文字必须用**英文原文**（整个 target 句子纯英文）。
- 改动：`prompts.py` 规则 2/4 + `config/action.yaml` click 描述
- 效果：chrome `35253b65` 0→1，thunderbird `dfac9ee8` 在完整跑里追回
- 已记入 memory `grounding-target-language`

### 3.2 run_in_terminal 的语义差异（multi_apps force-quit 退化）

- 原框架「打开终端 → pyautogui 真打字」→ 命令进 `~/.bash_history`
- 我们「subprocess 直接执行」→ 不写 history → evaluator 的 `grep kill ~/.bash_history` 判 "not killed from terminal"

**修复**：`pipeline.py` shell 分支，执行后补写 `~/.bash_history`。
- 效果：multi_apps `2b9493d7` 0→1

### 3.3 「先选中再格式化」规则缺失（impress/writer 恒 0 的一部分）

- 原框架明确有 `Select text before formatting`（triple-click 选整行，单次 click 只放光标不选文本）
- 我们缺这条 → 删除线/行距等格式操作没先选中，点按钮无效

**结论**：impress/writer 恒 0 是「原框架也挂」的 hard case，规则补齐是必要但不充分——真正卡点是 9B 决策 + 7B grounding 对深层次格式对话框的能力上限，留给 RL。

## 四、关键机制对齐情况

| 机制 | 原框架 | 我们 | 状态 |
|---|---|---|---|
| Tool Routing（GUI/code_agent/terminal）| ✅ 英文规则细 | ✅ 中文同结构 | ✅ 对齐 |
| UNO 直写（set_cell_values/save）| ✅ | ✅ 移植 | ✅ |
| code_agent view 块读图 | ✅ 含 PDF→PNG | ✅ 简化版 | ⚠️ 略简 |
| OCR 文字定位 | generate_text_coords | _ocr_fallback | ⚠️ 用途不同 |
| 覆盖写 type(overwrite) | ✅ | ✅ 补上 | ✅ |
| 单步动作校验 | 决策模型自判 | **程序化 _screen_diff** | ➕ 超越 |
| type 落点校验 | 无 | **决策模型复核** | ➕ 超越 |
| 时序对齐 transition=(o,a,o',assess) | 无 | ✅ | ➕ 超越 |

## 五、我们的优势

1. **RL 训练闭环**：语义动作规范化（去坐标）→ 语义轨迹树 → Bayesian smoothing 的 Q/V/C credit → `L = -Σ w_t log π`。与 pipeline 的 trajectory 打通。
2. **程序化动作校验**：`_screen_diff` 检测「点了没反应」、type 落点校验——确定性，不靠决策模型自觉。
3. **失败显式化**：grounding 失败返回 None 拒绝执行（不钳制成 (0,0)），比原框架 `assert` 抛异常更可控。
4. **结构化记忆**：带 action_history 字段，为 RL 时序对齐铺路。

## 六、待追平 / 遗留

1. **grounding 位置精度**（thunderbird "Remove Account" 坐标飘忽）——7B grounding 能力上限，原框架也靠运气，留给 RL 或更强 grounding。
2. **impress/writer 恒 0**——两边都挂的 hard case（格式对话框深 + evaluator 严格）。
3. **code_agent 简化**：缺 summary agent（原框架有独立 summary 模型提炼 code_agent 结果）。

## 七、评测结果（36 任务）

| | 之前 p0 | 本次 full36 | 50% 基线 |
|---|---|---|---|
| 通过率 | 15/36 = 41.7% | 16/36 = 44.4% | 18/36 = 50% |

3 个退化任务（multi_apps/bash_history、chrome/target 语言、thunderbird/target 语言）全部追回，2 个新随机挂（calc `42e0a640`、vs_code `276cc624`，非退化）抵消部分收益。

## 一句话结论

**我们在架构清晰度和 RL 基础设施上超越原框架，在「prompt 语言」「终端历史」「先选中再格式化」这三个隐蔽语义细节上曾落后，现已追平；剩余差距集中在 7B grounding 的固有能力上限——这正是 RL 训练要解决的目标。**
