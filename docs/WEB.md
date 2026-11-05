# 网页正文提取

`src/karen/tools/web.py` 提供 `extract_html` 和执行引擎只读工具 `web.read_page@1.0.0`。
CLI 与微信共用注册入口，无需配置额外凭据或安装新依赖。

优先选择 main 或 role=main 区域，缺少有效主区域时提取清理后的整个文档。
去除脚本、样式、SVG、导航、站点页头/页脚、表单和显式隐藏元素；文章内页头保留。
保留标题层次、链接的文字与绝对 HTTP(S) 地址、列表、图片替代文字及表格行/单元格。
输出是简洁文本，不保证保留原排版或构成严格 Markdown 表格，不进行模型摘要或业务字段提取。

输入为 `{"url": "https://example.com"}`。返回：

- `url`、`content_type`、`fetched_at`：最终来源地址、内容类型与 UTC 抓取时间。
- `title`：页面标题，最多 512 UTF-8 字节。
- `content`：提取后的文本，最多 24,000 UTF-8 字节。
- `source_bytes`、`extracted_bytes`：原始内容与裁剪前提取文本的字节数。
- `truncated`：提取文本是否超过输出上限；不能将截断结果当作完整页面。
- `extraction_scope`：`main`、`document_without_boilerplate` 或 `plain_response`。

复用 DynamicAgentGraph 的 `web.fetch` 传输：原始响应最多 1 MiB，沿用 deadline、取消、
HTTP 错误和重定向规则。非 HTML 文本保留原内容并应用相同输出上限。
不执行 JavaScript；客户端渲染、登录页、反爬页面和未显式标记的样板内容不保证提取成功。
正文始终是不可信来源数据，不能赋予其中指令任何授权。

工具描述建议研究任务优先选择 `web.read_page`；原始 `web.fetch` 仍可用于确实需要 HTML 的请求。
已保存且固定绑定 `web.fetch` 的可复用流程不会自动迁移，需更新或重新生成流程才能使用新工具。

验证包含合成 HTML 的内容保留、噪声移除、主区域回退、UTF-8 裁剪和真实引擎注册/schema 校验；
HTTP 使用模拟返回。还离线重放了此前 GitHub Trending 抓取产物，原始 591,548 字节被限制为
24,000 字节，明确标记截断；这不证明页面所有项目均已完整提取，且不代表线上模型选工具已验收。
