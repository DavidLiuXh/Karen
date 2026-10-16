# CLAMBER / LongMemEval 固定清单重跑（2026-10-13）

最新代码未提高整体通过率。本次完成当前固定公共回归清单全部 38 条：
CLAMBER 24 条、LongMemEval oracle 14 条。这里的全量指固定清单，不是公开数据集全集。
采用协议 2、自定义 DeepSeek 评分，不是官方 LongMemEval 榜单成绩。

| 测试集 | 10 月 10 日 baseline | 10 月 10 日 candidate | 本轮 |
| --- | ---: | ---: | ---: |
| CLAMBER | 12/24 | 13/24 | 12/24（50.0%） |
| LongMemEval oracle | 13/14 | 12/14 | 10/14（71.4%） |
| 合计 | 25/38 | 25/38 | 22/38（57.9%） |

对照为 `control-20261010-final/{baseline,candidate}` 的原始协议 2 结果，
不是能力诊断中固定 reader 的分数。版本及预算已有多项变化，也存在模型随机性和网络波动，
因此该表不能证明某一次修复单独导致升降。

## 本轮失败分布

CLAMBER：12 passed、11 failed、1 error。与 baseline 通过的案例集合相同。
公共标签动作：正确澄清 2、漏问 9、正确不追问 10、多问 2、运行错误 1。
必要澄清召回率为 2/12（含一条错误在分母），过度追问为 2/12。
`clamber-2050` 发生清晰度生成截断后阶段超时，保留为错误，没有择优重试覆盖。

LongMemEval：10 passed、4 error，没有最终 judge_error。失败如下：

| 案例 | 阶段 | 原因 |
| --- | --- | --- |
| b6019101 | 回答审查 | 草稿约 8.18 秒，审查耗尽共享 60 秒剩余的约 51.82 秒 |
| a4996e51 | 记忆写入 | 一条派生记忆为 MODEL_RESPONSE_INVALID，flush 失败；原文已持久化 |
| 75832dbd | 清晰度判断 | 60 秒阶段超时 |
| caf03d32 | 清晰度判断 | 60 秒阶段超时 |

b6019101、a4996e51、caf03d32 在 baseline 通过，本轮退步；75832dbd 原本已失败。
精排内部发生 7 次截断错误，部分扩预算重试仍耗尽阶段截止时间。
已返回的 10 个交互结果中，2 个含 RERANK_FAILED_FUSION_ORDER 且最终通过；不能据此
把失败且没有交互结果的案例当作未发生降级。最终 10/14 保留所有错误在分母。

## 代理检查

两批运行均在加载 .env 后，将 `api.deepseek.com,localhost,127.0.0.1` 加入大小写
NO_PROXY/no_proxy。未修改 .env 或系统代理。

运行结束后按相同环境重建生产 SDK 客户端，检查 HTTPX 实际路由：
DeepSeek 的同步/异步池分别为 ConnectionPool / AsyncConnectionPool；Tavily 分别为
HTTPProxy / AsyncHTTPProxy。该结果支持 DeepSeek 直连、Tavily 代理的配置已生效。
这是路由重建核验，不是已结束请求的抓包证据。trace 中也有连接读取错误，因此不能
断言所有等待都在模型计算；但已确认存在收到 HTTP 200 后输出截断、扩预算重试和
应用阶段截止时间耗尽，不能简单归因于代理。

## 运行完整性

Karen revision：`834686a`。两批两项目源码前后均 stable；并发均为 2，顺序执行两批。
Karen 源码 SHA256：`e61f7b94829cb2f61d4c276464b30ea65b6352dab456ea7506b2e8f5f5732413`。
DynamicAgentGraph 源码 SHA256：`ed81abadb8f497617808f8d2b7de0f5ba7bc45537565dc1a751ad33358939dc7`。
DynamicAgentGraph 已有未提交变更未修改，使用源码哈希标识实际运行代码。

结果目录（名称沿用启动时的 10 月 12 日，运行跨日）：

- `runs/evaluation/public-regression-20261012`：CLAMBER 24 条。
- `runs/evaluation/longmemeval-regression-20261012`：oracle 14 条；另含 transport-verification.json。
- 原始清单：`runs/evaluation/control-20261010-final/manifest-v2.json`。

首批命令使用了不匹配的 `longmemeval` suite 名称，因此实际只选择 CLAMBER；
随后按正确的 `longmemeval-oracle` 单独运行 14 条，未重复或覆盖任何案例。

## 运行结束后的预算调整

根据本轮超时证据，增加当前阶段的等待预算：

| 阶段 | 旧值 | 新值 |
| --- | ---: | ---: |
| 清晰度生成及审查，共享 | 60 秒 | 120 秒 |
| 回答生成及证据审查，共享 | 60 秒 | 120 秒 |
| 目标生成及证据审查，共享 | 60 秒 | 120 秒 |
| 精排，含截断重试 | 60 秒 | 90 秒 |
| 召回全流程 | 120 秒 | 180 秒 |

输入路由、查询分析、记忆抽取、输出 token 上限及模型模式保持原设置。
重试和审查继续共享原阶段截止时间，不为每个子调用重置总预算。
这会增加最坏等待时间，不保证消除模型或网络异常，也不处理非法记忆输出。
相关离线测试 248 项通过。此调整发生在本次完整评测结束后，**不属于上表版本**；
尚未以新预算重新完成公共评测，不能将 22/38 说成新预算成绩或宣称超时已根治。
