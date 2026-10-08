# 私人秘书 · 架构契约

> 本文件是**唯一的设计来源**。代码与它冲突时，以本文件为准；要改设计，先改本文件。
> 规矩：本文件只写设计与规则，不写实现。实现一律在 `app/` 下。

版本：v1（2026-10-08）

---

## 一、定位

把 QQ 群消息、发给小号的消息、手动粘贴/拖入的文件，变成待办，定时汇总推送到企业微信。

- 成功标准：干净 Windows 上从下载到收到第一条企微推送 ≤ 10 分钟，全程不碰命令行。
- 最低配置：Windows 10/11、4 GB 内存、无需独显。
- 发布形态：免安装绿色文件夹（PyInstaller onedir），不打单文件 exe。

---

## 二、目录树

```
personal-secretary/
├── CONTRACT.md              本文件
├── README.md                给用户看的说明
├── run.py                   开发期命令行入口（第 9 步被界面取代）
├── app/
│   ├── contracts/           ★ 契约层：唯一被所有模块共享的东西
│   │   ├── models.py        四种数据格式
│   │   └── interfaces.py    五个接口
│   ├── sources/             第一段：来源
│   ├── pipeline/            第二段：理解
│   ├── store/               第三段：存储
│   ├── sinks/               第四段：输出
│   └── runtime/             运行时：配置、插件、调度、日志
├── tests/                   每步门禁 + 回归语料
└── tools/                   一次性脚本（基线测量、迁移等）
```

**模块隔离铁律**

1. `sources / pipeline / store / sinks` 之间**只准传 `contracts/models.py` 里的四种格式**。
2. 禁止 import 别的模块的内部文件。需要共用逻辑就上提到 `contracts/` 或 `runtime/`。
3. 任何一个模块或插件加载失败 → 主程序照常启动，并在界面标出来。不允许因为一个零件崩掉整个程序。

**各段零件与执行顺序**

| 段 | 零件 | 顺序 |
|---|---|---|
| 来源 | 手动录入、QQ 群消息、小号收件箱、（插件）邮箱 | 无关 |
| 理解 | 闲聊过滤 → 时间识别/切分 → 去重 →（插件）AI 增强 | 5 → 10 → 30 → 90 |
| 存储 | SQLite | 无关 |
| 输出 | 企微群机器人、控制台、（插件）课表、语音 | 无关 |

**来源只做两件事**：判断"这个会话在不在接收范围里"（群白名单 / 收件箱名单），
以及把协议报文拍成人话。**不做内容判断。**

> 修订（第 4 步）：群噪声过滤从「来源」移到「理解」，排在最前面（order 5）。
> 原因：它要用时间识别（判断"只有时间没有事情"），而时间识别属理解层；
> 留在来源里会让 sources 依赖 pipeline，破坏隔离。
> 副作用是好的：被拦下的群消息现在也会存进 `messages` 表，
> 「为什么这条没收下」从此查得到。

**理解层的否决约定**：零件想表达"这条消息到此为止"，就写
`message.raw["skip_reason"] = "原因"`，管线看到标记立刻停下，后面的零件一律不跑。

> 为什么非要有这个标记（第 4 步踩的坑）：过滤器判"这是闲聊"时返回空列表 `[]`，
> 可下一个零件看到 `[]` 会以为"还没轮到它"，于是又老老实实解析了一遍 ——
> 闲聊、吐槽、碎片全都进了待办，过滤器形同虚设。
> **"拒绝"和"还没轮到"用同一个空列表表达，是分不出来的。**

---

## 三、四种数据格式

时间统一用字符串 `"YYYY-MM-DD HH:MM:SS"`，不用 datetime 对象过界（避免时区、格式、序列化三处打架）。

### 1. `Message`（消息）— 来源 → 理解

| 字段 | 类型 | 说明 |
|---|---|---|
| `msg_id` | str | 全局唯一。格式 `来源kind:原始id`，去重用 |
| `source_kind` | str | `manual` / `qq_group` / `qq_private` / `qq_inbox` / `email` / 插件名 |
| `source_name` | str | 群名 / 邮箱名 / `"手动录入"` |
| `sender` | str | 发送人昵称或号码 |
| `is_self` | bool | 是不是自己（本机小号）发的 |
| `sent_at` | str | 消息时间 |
| `text` | str | 正文（附件提取的文字会由理解层追加进来） |
| `attachments` | list[`Attachment`] | 附件 |
| `raw` | dict | 原始载荷，只用于排查，业务逻辑不许依赖 |

### 2. `Attachment`（附件）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` / `path` | str | 文件名 / 本地绝对路径 |
| `mime` / `size` | str / int | 类型 / 字节数 |
| `kind` | str | `image` / `document` / `other` |
| `sha256` | str | 内容指纹，去重用 |

### 3. `TaskDraft`（待办草稿）— 理解 → 存储

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | str | 标题，≤200 字 |
| `detail` | str | 正文/网址/原文，给详情面板用 |
| `due_at` | str \| None | 截止时间；只说了哪天没说几点 → 当天 23:59 |
| `location` | str \| None | 地点 |
| `remind_level` | int | 0 只记录 / 1 普通 / 2 强提醒 / 3 极重要 |
| `priority` | int | 调度优先级，0 最高 |
| `source_kind` / `source_name` / `sender` | str | 溯源，推送时要显示 |
| `origin_msg_id` | str | 来自哪条消息 |
| `attachments` | list[`Attachment`] | 关联附件 |
| `confidence` | float | 0~1，可信度。< 阈值则进废件箱 |
| `quoted` | str | 原始消息片段（推送时带上，方便看出为什么被判成待办） |

### 4. `Task`（待办）— 存储 → 输出

草稿全部字段，外加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | int | 主键 |
| `status` | str | `open` / `done` / `dropped` |
| `is_archived` | bool | 是否归档 |
| `dedup_key` | str | 去重键 |
| `created_at` / `updated_at` / `completed_at` | str | 时间戳 |
| `archive_reason` / `drop_reason` | str | 归档/丢弃原因 |

### 5. `PushPayload`（推送内容）— 输出层内部

| 字段 | 类型 | 说明 |
|---|---|---|
| `channel` | str | `wecom_bot` / `console` |
| `kind` | str | `text` / `markdown` |
| `title` / `body` | str | 标题 / 正文 |
| `attachments` | list[`Attachment`] | 一起发出去的附件 |
| `note` | str | 降级/异常说明，会显示给用户 |

---

## 四、接口签名

五个接口，全部在 `app/contracts/interfaces.py`。实现它们的类**不许被别的模块 import**，
只能通过 `runtime/` 里的注册表拿。

### `Source` — 来源

```
@rule  一个来源只负责「把外部世界的东西变成 Message」，不做任何解析
@inputs 外部事件 / 定时拉取
@outputs Message
kind: str            唯一标识，同时是 Message.source_kind
title: str           界面上显示的名字
submit(message) -> Message     外部把消息塞进来（QQ 推送、界面粘贴）
fetch() -> list[Message]       主动拉取（邮箱用），默认返回空列表
```

> 一个来源可以产出**多种** `source_kind`。QQ 源就是这样：群消息标成
> `qq_group`、别人的私聊标成 `qq_private`（这两种都要过闲聊过滤），
> 小号收件箱标成 `qq_inbox`（不过滤）。
> `Source.kind` 是来源的标识（`"qq"`），和消息上的 `source_kind` 不是一回事。

### `TextEnricher` — 理解层·消息级零件

```
@rule  只把消息本身变丰富，不产生待办
@inputs Message
@outputs Message（原地补充 text / attachments）
name: str
order: int           执行顺序，小的先跑
enabled() -> bool    关掉就不执行
process(message) -> Message
```

### `DraftEnricher` — 理解层·草稿级零件

```
@rule  吃消息、吐草稿；或对已有草稿做加工。链式执行，上一步的输出是下一步的输入
@inputs Message + list[TaskDraft]
@outputs list[TaskDraft]
name: str
order: int
enabled() -> bool
process(message, drafts) -> list[TaskDraft]
```

固定顺序：**切分（order 10）→ 时间识别（20）→ 去重（30）→ AI 增强（90，插件）**。
每步可单独 `enabled()` 关掉，也可被插件替换。

### `Store` — 存储

```
@rule  只读写数据库，不放业务规则（业务规则在 pipeline 里）
save_message(message) -> None
save_drafts(message, drafts) -> list[Task]
list_tasks(view, limit) -> list[Task]     view: open/today/tomorrow/week/overdue/archive/junk
set_status(task_id, status, reason) -> None
log_dropped(draft, reason) -> None         废件箱 + 删除记录（日后调优的案例）
```

### `Sink` — 输出

```
@rule  只负责把 PushPayload 送出去，不改内容
name: str
deliver(payload) -> SinkResult(ok, code, message)
```

---

## 五、阈值表（全部写在 `runtime/config.py`，代码里不许出现裸数字）

| 键 | 默认 | 说明 |
|---|---|---|
| `parse.filter_strength` | `strict` | `strict` / `loose` / `off` 三档，**只给这一个开关** |
| `parse.confidence_min` | `0.45` | 低于此值进废件箱 |
| `parse.merge_window_seconds` | `60` | 同群同人这个时间内连续发的消息合并成一条 |
| `parse.due_default_hour` | `23:59` | 只说了哪天没说几点时的兜底时刻 |
| `title.max_len` | `200` | 标题上限 |
| `title.min_meaningful_chars` | `2` | 摘掉时间和编号后少于这么多汉字 → 是碎片 |
| `store.attachment_retention_days` | `30` | 附件保留天数 |
| `store.keep_dropped_days` | `90` | 废件箱记录保留天数 |
| `push.default_times` | `["07:30","21:30"]` | 默认一天两次汇总，用户可改 |
| `push.quote_max_chars` | `80` | 推送里带的原文片段长度 |
| `push.fallback_text_chars` | `300` | AI 不可用时的原文保底长度 |
| `ai.mode` | `conservative` | `conservative` / `aggressive` |
| `ai.cloud_priority` | `true` | 默认优先云端 |
| `ai.token_budget_per_day` | `0` | 0 = 不限制 |
| `resource.mem_critical_percent` | `88` | 内存占用超过此值：只入库、不分析、不推送 |
| `resource.mem_resume_percent` | `75` | 降到这个值以下恢复 |
| `risk.require_secondary_account` | `true` | 首次启动必须确认是用小号 |
| `wecom_bot.enabled` | `false` | 关着的时候推送会退回控制台，不会静默丢消息 |
| `wecom_bot.webhook` | `""` | 用户唯一需要填的一项；默认空，新仓库零真实信息 |
| `wecom_bot.max_per_minute` | `20` | 官方硬限制，改了也没用，列在这里是为了看得见 |
| `wecom_bot.retry_times` | `2` | 网络抖动重试次数 |
| `wecom_bot.timeout` | `10` | 单次请求超时秒数 |
| `wecom_bot.send_attachments` | `true` | 推送时要不要把图片/文件原件一起发到手机 |
| `qq.enabled` | `false` | 关着时完全不监听端口 |
| `qq.bind_host` | `127.0.0.1` | 只监听本机回环；即使路由器映射了端口，外面也连不进来 |
| `qq.bind_port` | `18080` | 和 NapCat 那边配的端口必须一致 |
| `qq.path` | `/onebot/v11/ws` | 同上，路径也要一致 |
| `qq.token` | `""` | 首次启用自动生成；空着就拒绝所有连接 |
| `qq.group_ids` | `[]` | 要监控的群号。**默认空 = 一个群都不收**，必须显式勾选 |
| `qq.receive_all` | `false` | `true` = 所有群都收（不推荐：噪声和 AI 费用都会失控） |
| `qq.private_ids` / `qq.receive_private_all` | `[]` / `false` | 私聊同理 |
| `qq.inbox_ids` | `[]` | 小号收件箱：自己发给这些会话的消息也变待办（微信/短信转发靠它） |
| `qq.only_mention` | `false` | `true` = 群里只有 @我 的才收 |
| `qq.max_text_chars` | `3000` | 单条消息截断长度，防超长轰炸 |

---

## 六、降级映射表（每条都要有可量化的验证方法）

| 触发条件 | 降级行为 | 怎么证明没坏 |
|---|---|---|
| 云端 AI 超限 / 连不上 | 回退规则解析，**照常推送**，并在推送里注明"AI 连不上了" | 断开网络跑一次，必须仍然收到推送且带提示 |
| 云端 AI 不可用且用户装了本地模型 | 转本地模型；仍然失败则按上一行 | 关掉云端配置，本地模型必须接手 |
| AI 完全不可用 | 推送原文前 `push.fallback_text_chars` 字，标明原文过长 | 长文档在无 AI 时不能"什么都没收到" |
| 长文档且无 AI | 整份存成**一条**待办，不逐行拆 | 20 页 PPT 只能产生 1 条，不是几十条 |
| 过滤强度=`loose` | 放宽碎片判定，宁可多收 | 之前被丢的碎片要重新出现 |
| 过滤强度=`off` | 关闭碎片过滤（用户说有暗号） | 所有行都入库 |
| 插件加载失败 | 跳过该插件，主程序继续跑，界面标红 | 故意写坏一个插件，程序仍能启动 |
| 附件文件缺失 | 跳过附件，待办照常保留 | 删掉附件文件后再推送，不报错 |
| QQ 版本不匹配 | 明确告诉用户"版本不对，需要装 XXX 版"，不静默失败 | 用不匹配版本启动，必须看到人话提示 |
| 被杀软删文件 | 检测到文件缺失时提示"被杀软删了，点这里看怎么办" | 手动删掉 NapCat 文件，必须看到提示 |
| 内存超 `mem_critical_percent` | 只入库、不分析、不推送 | 占满内存后程序不卡、不崩 |

---

## 七、关键行为规则（红线，写死在代码里）

1. **摘掉时间和编号后什么都不剩 → 判为碎片，不回退成原文。**
   历史事故：旧项目标题为空时会照抄原文，导致纯时段表 `10月9日-10月21日 12:20-13:50` 变成待办标题。
2. **排班表/值班表（多行同构）→ 整段合成一条**，原文进 detail。不做逐行拆分。
3. **用户填的名字出现在表里 → 那几行单独拎出来强调。**
4. **碎片进废件箱**（不推送、不进主列表），写日志，保留 `store.keep_dropped_days` 天。
5. **自己发的（小号收件箱、手动录入）宽判，群里和截图来的严判。**
   证据：`拿快递`、`打游戏` 是合格短待办，一条都不许误伤。
6. **推送必须带原始片段**；每条待办都能看出"为什么被判成待办"。
7. **云端上传默认关闭**，首次开启时明确告知内容会被发到第三方。
8. **AI 摘要必须标「【AI 整理，可能有误】」**，原文和附件永久保留本地。
9. **待办判定全自动**，不设确认环节；补偿手段 = 推送带原文 + 一键删误判 + 删除记录留存。
10. **风控**：文档第一行如实说明「小号 vs 大号」的取舍与代价（**不许写"必须用小号"——
    作者自己用的就是大号，写"必须"是假话**）；首次启动弹风险确认；设置里醒目重复提醒。
    程序侧能做的降低风险手段：只收不发、只监控勾选过的会话、不拉群成员列表。

---

## 八、验收断言

每步完成必须满足：

1. `python -m unittest discover -s tests` 全绿。
2. 该步的专项自检脚本跑绿。
3. 端到端数字指标：

**识别准确率（第 6 步起生效，用旧库 168 条真实数据当基准集）**

```
断言：碎片率 < 3%
      其中「摘掉时间和编号后什么都不剩」的条目占比
      改前基准 16%（27/168），由 tools/baseline_scan.py 测量

断言：短待办零误伤
      「拿快递」「打游戏」「取快递」「耍起」「买牛奶」这类必须 100% 保留
      —— 注意：**不含**「学号」「格式」「姓名」这类表单栏名。
      它们各自成条是误判，不是要保护的对象（见下一条）。

断言：表单栏名不成条
      「学号」「格式」「截止时间：10月3日 07:41」「联系人：李老师」必须 0 条

断言：排班表不再碎
      旧库里 (A组) (B组) ... 📅周一 这种条目，改后必须 < 1 条
```

---

## 九、开发顺序

| 步 | 内容 | 门禁 |
|---|---|---|
| 0 | 出契约（本文件） | 你确认 |
| 1 | 空壳骨架：四种格式 + 五个接口 + 全部空壳 | `test_contract_integrity` |
| 2 | 第一条竖直切片：粘贴文字 → 规则解析 → 存库 → 打印 | `test_parse_basic` + 手动跑通 |
| 3 | 接企微推送 | 手机上真收到消息 |
| 4 | 接 QQ 来源 | 群里发一条，能进库 |
| 5 | 文件解析 + 附件 | 图片/Word/PPT/PDF 变待办；附件推手机 |
| 6 | 去重 + 时间解析增强（修 16% 碎片） | **碎片率 < 3%，短待办零误伤** |
| 7 | 定时调度（早报 + 晚间） | 到点自动推 |
| 8 | 一键部署脚本（自动装 QQ + NapCat + 二维码） | 干净机器上 10 分钟跑通 |
| 9 | 界面（克制风） | 全按钮走查无异常弹窗 |
| 10 | 插件系统 | 坏插件不影响启动 |
| 11 | 课表插件 | 第一个插件跑通 |
| 12 | 打磨 + 文档 + 发布准备 | 3 个外部用户装上用过 |

---

## 十、每条决策的出处

本契约的每条规则都对应一次明确的拍板，不是推断出来的：

| 决策 | 出处 |
|---|---|
| 13 项内置 / 4 个插件 / 砍掉清单 | 第二轮问答，全认可 |
| 四段流水线 + 固定骨架每步可开关 | 第三轮问答 |
| 先命令行跑通再套界面 | 第三轮问答 |
| 踩坑算法直接搬 | 第三轮问答 |
| AI 默认云端、连不上回退规则并告知 | 第四轮问答 |
| AI 总结双模式、默认保守 | 第五轮问答 |
| 排班表整段合成 + 用自己名字强调 | 第六轮问答 |
| 碎片进废件箱 + 三档总开关 | 第六轮问答 |
| 待办判定全自动 + 推送带原文 | 第六轮问答 |
| 推送默认一天 2~3 次、时间可设 | 第四轮问答 |
| 风控写死必须用小号 + 启动提示 | 第四轮问答 |
| 自装匹配版本 QQ + 沙盒可选 + 杀软文档提示 | 第七轮问答 |
| 新建仓库、旧项目不动、不迁数据、零真实信息 | 第五、六轮问答 |
| 碎片率 < 3% 且短待办零误伤 | 第八轮问答 |
| 每步交付验收 | 第八轮问答 |
