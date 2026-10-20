---
name: ilearning
description: 帮用户自动完成 iLearning（华为学习平台）的课程。当用户提供一个 ilearning.huawei.com 的课程链接，并希望"学完/完成/刷完"这门课时使用。用内置的 playwright MCP 在用户自己的浏览器里自动操作，课程记到用户自己的账号。
---

# iLearning 课程自动完成

用**内置的 playwright MCP**（`browser_*` 工具）在用户自己的浏览器里自动完成 iLearning 课程。

核心逻辑已封装在 `scripts/complete_course.js`（一个 `async (page) => {...}` 函数）。
你只做三件事：**复制脚本 → 打开课程页 → 执行脚本**。不要自己现编翻页/答题逻辑。

## 执行流程（严格按顺序，一步紧接一步）

### 1. 复制脚本到工作目录

用 Bash 把脚本复制到当前工作目录（`browser_run_code_unsafe` 的 filename 只认工作目录）：

```sh
cp "<本skill目录>/scripts/complete_course.js" ./complete_course.js
```

### 2. 打开课程页（并确认登录）

用 `browser_navigate` 打开用户给的课程 URL，`browser_snapshot` 确认已登录（页面显示用户自己的名字，而非登录框）。
若落在登录页，让**用户本人登录**，再 `browser_navigate` 回到课程 URL。

### 3. 立即执行脚本

紧接上一步，**马上**用 `browser_run_code_unsafe` 执行，`filename` 参数填 `./complete_course.js`。
第 2、3 步之间**不要**读文件、不要做任何其它操作（否则页面会失效变成 `about:blank`，导致报错 "Target page has been closed"）。

执行后就是**阻塞等待**，直到脚本全部跑完才返回。期间：

- **禁止**调用任何浏览器工具（`browser_wait_for` / `browser_snapshot` / `browser_evaluate` / `browser_console_messages` / `browser_navigate` / `browser_tabs` 等）——它们会打断脚本、把页面弄乱；
- 脚本的日志在 server 端，`browser_console_messages` 看不到，不要去查；
- 跑几分钟到几十分钟是正常的，耐心等。

### 4. 报告结果

脚本跑完会返回一个结果对象，**直接按它报告，不要再复查**：

- `ok: true` → 报告"已完成全部 X/Y 章"。
- `ok: false` → 报告 `completedAfter/total`、`remaining`（未通过章节）、`failed`、`stoppedReason`。
- `stoppedReason === 'login'` → 让用户先登录再重试。

仅当确实没拿到返回对象时，才允许做**一次**确认：`browser_navigate` 重新打开课程页，等目录加载，`browser_snapshot` 数绿勾，报告"完成 X/Y 章"，然后立即停止。

## 注意

- 脚本里已含全部细节：目录扫描、绿勾检测、视频快进+降速重播、PDF 内部滚动、Qwen 答题、问卷填写、逐章验证。不要自己额外操作。
- 视频章节若重播后仍无绿勾，是平台"真实观看时长"防作弊，属正常限制，如实报告。
- 全程只用用户自己的浏览器，不切换账号。
