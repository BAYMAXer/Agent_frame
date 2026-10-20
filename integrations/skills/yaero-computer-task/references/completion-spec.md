# 可选的显式验收条件

`start_task.completion_spec` 的 MCP schema 仅声明对象类型，内部字段还会由 Yaero 的验收器校验。默认省略该参数，让框架根据完整 `task` 形成验收要求。只有用户要求能被所列条件完整覆盖时，才提交显式条件；不要把包含填写、下载或发送等要求的任务简化为“打开页面”。

以下两个检查类型已由当前验收器支持。不要根据它们猜测其他类型或字段。

| 类型 | 必需字段 | 可选字段 | 核查含义 |
| --- | --- | --- | --- |
| `url_equals` | `kind`、`url` | `id`、`scope` | 当前页面 URL 等于指定的完整 HTTP(S) URL |
| `text_present` | `kind`、`text` | `url`、`id`、`scope` | 当前页面可访问性节点的完整 `name` 或 `text` 与指定文字匹配 |

对象顶层只能有 `checks` 数组。`id` 是可选的非空字符串，同一数组内不能重复。`scope` 默认为 `final`，表示最终观察中仍需满足；`milestone` 表示执行过程中曾观察到即可，只有用户要求确实如此时才使用。

URL 校验不支持通配符。路径、查询和片段参与比较；入口地址会重定向时，不要把入口地址误当作用户要求的最终地址。URL 不得包含用户名或密码。

`text_present` 会合并连续空白后比较整个节点文字，区分大小写，不是子串搜索，也不是截图 OCR。缺少有效浏览器可访问性观察时，检查可能返回 `unknown`。通常同时提供 `url`，避免在其他页面找到相同文字而误判。

例如用户明确要求：“打开 `https://example.com/`，最终留在该页面，并确认页面包含完整文字 `Example Domain`”，可以传入：

```json
{
  "checks": [
    {
      "id": "page",
      "kind": "url_equals",
      "url": "https://example.com/"
    },
    {
      "id": "heading",
      "kind": "text_present",
      "url": "https://example.com/",
      "text": "Example Domain"
    }
  ]
}
```

这是条件示例，不是已经观察到的事实。完整原始要求仍需写入 `task`。没有说明的复杂要求继续保留在 `task` 中，并省略 `completion_spec`；不要试填未知字段或用空 `checks` 代替用户要求。
