# 私人秘书（personal-secretary）

把 QQ 群消息、发给小号的消息、以及手动粘贴/拖入的文件，变成待办，定时汇总推送到企业微信。

> ⚠️ **关于用哪个 QQ 号：自己权衡，风险是真的。**
>
> 本工具通过非官方方式（把 NapCat 注入 QQ）让电脑登录 QQ 来收消息。
> 腾讯随时可能限制这种登录方式，**被封的风险落在你登录的那个号上**。
>
> | | 优点 | 代价 |
> |---|---|---|
> | **小号**（推荐） | 封了不心疼 | 得先进你要监控的那些群；学院群、班级群未必让外人进 |
> | **大号** | 群都加好了，装上就能用 | 风险落在主号上 |
>
> **作者自己用的是大号**，从 2026 年 9 月下旬稳定用到现在，没出过事。
> 这是经验，**不是保证** —— 别把它当成"大号安全"的证据。
>
> 想降低风险：**只收不发**（本工具就是这么设计的，不会主动发任何 QQ 消息）、
> 只勾选真正需要监控的群、别频繁重启登录。

---

## 现在做到哪一步了

项目按**契约先行**的方式推进：先把设计钉死，再一块一块填肉，每块都有自动检查兜底。
进度见 [CONTRACT.md](CONTRACT.md) 第九节。

| 步 | 内容 | 状态 |
|---|---|---|
| 0 | 架构契约 | ✅ 完成 |
| 1 | 空壳骨架 + 契约自检 | ✅ 完成 |
| 2 | 第一条竖直切片：粘贴文字 → 解析 → 存库 → 打印 | ✅ 完成 |
| 3 | 接企微推送 | ✅ 完成 |
| 4 | 接 QQ 来源 | ✅ 完成 |
| 5 | 文件解析 + 附件 | ✅ 完成 |
| 6 | 去重 + 时间解析增强（碎片率 <3%） | ✅ 完成（实测 0%） |
| 7 | 定时调度（早报/晚间） | ✅ 完成 |
| 8 | 一键部署（官方 OneKey 安装器） | ✅ 完成 |
| 9 | 界面（网页界面，TickTick 那种克制风） | ✅ 完成 |
| 10 | 插件系统 | ⬜ 待做 |
| 11 | 课表插件 | ⬜ 待做 |
| 12 | 打磨 + 文档 + 发布 | ⬜ 待做 |

---

## 现在就能跑

不需要装任何东西（Windows + Python 3.11 以上即可）：

```bat
python run.py --selftest          检查契约和环境
python run.py "明天下午三点交作业"  录一条
python run.py --read 照片.jpg     只看这个文件能提出什么字，不入库
python run.py --file 作业.pptx    把一个文件变成待办
python run.py --list              看未完成
python run.py --list junk         看废件箱（没收下的原文都在这里）
python run.py --digest            生成一份汇总打印出来
python run.py --push              把汇总真正推送到企业微信
python run.py --check-push        检查推送通道配置（不发消息）
python run.py --next-push         看下一次什么时候推、有没有错过的
python run.py --schedule          启动定时推送（Ctrl+C 停止）
python run.py --setup --account 你的QQ号   只装 NapCat（约 2.5 分钟）
python run.py --deploy --account 你的QQ号  装 + 启动（先起接收端再起 QQ）
python run.py --ui                打开界面
python run.py --shortcut          在桌面建快捷方式
python run.py --check-qq          看 QQ 接入的配置和状态
python run.py --qq                启动 QQ 接收（Ctrl+C 停止）
python run.py --done 3            标记某条完成
python run.py --stats             看统计
```

跑测试（标准库 unittest，不用装 pytest）：

```bat
python -m unittest discover -s tests -t . -v
```

---

## 想改代码？先看这几张地图

每个文件开头都有一张「想改这里的话，先看这张地图」，直接告诉你改什么去哪一行：

| 你想改 | 去哪 |
|---|---|
| 界面上的分组口径（今天/本周怎么算） | `app/store/repository.py` 的 `_view_filter()` |
| 怎么判断一句话是待办 | `app/pipeline/quick_parse.py` |
| 什么样的碎片该丢掉 | `app/pipeline/quick_parse.py` 的 `is_fragment()` |
| 过滤的松紧 | `config.toml` 的 `[parse] filter_strength`（严格/宽松/关闭三档） |
| 推送长什么样 | `app/sinks/console.py` 的 `build_digest()` |
| 推送时间 | `config.toml` 的 `[push] times` |
| 错过了还补不补 | `config.toml` 的 `[push] catch_up_minutes` |
| 什么时候暂停推送 | `app/runtime/sysmon.py` 的 `busy()` |
| 加一个新的消息来源 | 新写一个 `app/sources/xxx.py`，然后在 `run.py` 的 `build()` 里注册 |
| 加一个新推送通道 | 新写一个 `app/sinks/xxx.py`，然后在 `app/sinks/router.py` 里登记 |
| 群里什么算闲聊、什么算通知 | `app/pipeline/chat_filter.py` 里的那几张词表 |
| QQ 怎么连（端口、令牌、收哪些群） | `config.toml` 的 `[qq]` 段 |
| 识别用哪个 OCR 引擎 | `config.toml` 的 `[extract] ocr_engine` |
| 支持一种新文件格式 | `app/pipeline/extract/service.py` 的 `_READERS` 加一行 |
| 多线程下数据库行为 | `app/store/repository.py` 的 `_synchronized` —— **改前先读那段注释** |

改完一定要跑一遍：

```bat
python -m unittest discover -s tests -t .
```

---

## 为什么值得一看

这个项目从旧版本改造而来。旧版本自己用了几个月，攒下了真实的踩坑记录，
新版本把它们全部继承下来，其中最值钱的一条：

**"经常只有时间"** —— 旧库 168 条待办里有 27 条（16%）摘掉时间后什么都不剩，
手机收到的就是 `（A组）`、`📅周一`、`10月9日-10月21日 12:20-13:50` 这种碎片。
三个根因已经定位并写进契约，第 2 步就修掉了最致命的一个（标题为空时回退成原文）。

完整清点见 [CONTRACT.md](CONTRACT.md) 第七节。

---

## 架构

```
[来源] ──► [理解] ──► [存储] ──► [输出]
```

四段之间**只通过契约层定义的四种数据格式通信**，不许互相 import 内部实现 ——
这条由 `tests/test_contract_integrity.py` 自动检查，破坏边界会直接测试失败。

```
app/contracts/   四种数据格式 + 五个接口
app/sources/     手动录入、QQ 群消息、小号收件箱
app/pipeline/    闲聊过滤、繁简归一、规则解析、管线
app/store/       SQLite
app/sinks/       企微群机器人、控制台
app/runtime/     配置中心（所有阈值集中在这里，代码里不写裸数字）
```

---

## 许可

待定。
