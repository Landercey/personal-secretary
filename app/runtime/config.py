"""配置中心：所有阈值只在这里定义。

=====================================================================
想改这里的话，先看这张地图
=====================================================================
  改默认值（过滤强度、保留天数、推送次数…）→ 下面的 DEFAULTS
  加一个新配置项                            → 在 DEFAULTS 里加一行，
                                              然后业务代码用 config.get() 读
  改配置文件长什么样                        → `render_toml()`
  改配置文件存放位置                        → `default_root()` / `default_config_path()`

**铁律**：业务代码里不许出现裸数字。要调行为就改这里，或者改用户的
config.toml。这样以后想调参不用翻代码。

=====================================================================
CONTRACT.md 第五节：代码里不许出现裸数字。要调行为就改这里的 DEFAULTS，
或者改用户目录下的 config.toml。

配置文件用 TOML：
  · 读取用标准库 tomllib（Python 3.11+ 自带，零依赖）；
  · 写回时按段重新生成，注释保留是第 9 步（界面配置面板）的事。
"""

from __future__ import annotations

import os
import re
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 默认值。所有键都带注释说明，改这里等于改行为。
# --------------------------------------------------------------------------
DEFAULTS: dict[str, dict[str, Any]] = {
    "parse": {
        # 过滤强度总开关：strict / loose / off。只给这一个开关，不给十几个细开关。
        "filter_strength": "strict",
        # 这里**故意没有**"可信度低于 X 就丢掉"这一项。
        # 判"值不值得变待办"和"是不是碎片"已经有两道闸门了
        # （pipeline/actionable.py + quick_parse.is_fragment），
        # 再加一个打分阈值不会更准，只会让"这条为什么没收"要多跨一道门槛才解释得清。
        # TaskDraft.confidence 字段保留 —— 以后 AI 插件用它表达"我有多确定"。
        # 同群同人这个秒数内连续发的消息合并成一条待办。
        "merge_window_seconds": 60,
        # 只说了哪天没说几点时的兜底时刻（不写 09:00：一到九点就标红逾期，
        # 但用户说的明明是"今天"）。
        "due_default_hour": "23:59",
        # 你自己的名字/昵称。值班表、排班表里含这些词的行会被单独拎出来，
        # 你不用在整张表里找"我周几值日"。填多个就分开写。
        "my_names": [],
        # 同一个人在同一个会话里，这么多秒内连发的消息算同一件事。
        # 典型场景：先发一句"明天要交作业"，紧接着发一张作业要求的图。
        "merge_window_seconds": 60,
    },
    "title": {
        "max_len": 200,
        # 摘掉时间和编号后少于这么多汉字 → 判为碎片。
        # 这个数字是"拿快递""打游戏"和"（A组）""5G"之间的分界线。
        "min_meaningful_chars": 2,
    },
    "store": {
        "attachment_retention_days": 30,
        # 废件箱记录保留多久。删除记录是日后调规则的案例，不许马上丢。
        "keep_dropped_days": 90,
    },
    "extract": {
        # OCR 引擎：
        #   auto    装了 RapidOCR 就用它，否则用 Windows 自带（默认）
        #   rapid   只用 RapidOCR
        #   windows 只用 Windows 自带
        # 实测 RapidOCR 明显更准（手写黑板也能读），但要拖进 390 MB 的
        # onnxruntime，所以做成可选。装法：pip install rapidocr_onnxruntime
        "ocr_engine": "auto",
        # PDF 最多处理多少页。几十页的教材整本 OCR 会跑到天荒地老。
        "max_pages": 20,
        # 扫描件渲染倍数。2 倍对 OCR 够用；调大更清楚但更慢更吃内存。
        "render_scale": 2.0,
        # 单张图片识别的超时（秒）。系统 OCR 一张约 1 秒，留足余量。
        "ocr_timeout_seconds": 180,
        # 单个附件超过多少 MB 就不处理，防止吃光内存。
        "max_attachment_mb": 30,
    },
    "push": {
        # 推送总开关。关掉之后一条都不会自动推（手动 --push 还能用）。
        "enabled": True,
        # 每天什么时候推汇总，24 小时制，想推几次就写几个。
        # 标题按时间点自动定：中午前叫"今天要做的"，傍晚后叫"明天要留意的"。
        "times": ["07:30", "21:30"],
        # 错过了还补推吗：开机比推送时间晚了多久以内，还补这一次。
        # 早上 7:30 关机、9:00 才开 → 这份早报还值得推（差 90 分钟）。
        # 晚上 10 点才开 → 再推"今天的早报"就是纯打扰，不推。
        # 填 0 = 错过就永远不补。
        "catch_up_minutes": 120,
        # 免打扰：这段时间**不发临期提醒**（汇总照发 —— 汇总时间是用户自己挑的）。
        # 格式 "23:00-07:30"，留空 = 关掉。
        "quiet_hours": "23:00-07:30",
        # 推送里带的原文片段长度。
        "quote_max_chars": 80,
        # AI 不可用时的原文保底长度。
        "fallback_text_chars": 300,
    },
    "remind": {
        # 提醒力度：**详情里那个「提前多久提醒」的下拉里能选的档**（分钟）。
        # 比如 [60, 30, 10, 3] = 截止前 60/30/10/3 分钟各是一个选项。
        # 1440 = 提前一天。列表里不用写 0 —— 下拉里永远有一个「默认」，
        # 默认 = 不单独提醒，只在每日汇总里出现。
        # 每条待办自己选哪个，在它的详情里改；没选的都按「默认」。
        "options": [60, 30, 10, 3],
        # 新待办默认带哪几档提醒（用户原话："默认是全选，不然都不提醒"）。
        # 留空 [] = 新待办一声不响，只在每日汇总里出现。
        # 想安静点就删掉几档，比如 [30, 10]；每条的选项还能在详情里单独改。
        "default_leads": [60, 30, 10, 3],
        # 课表单独一套：上课前多少分钟响一次。默认只在课前 10 分钟响一次
        # （一天四五节课，配太多档会变成轰炸）。
        "timetable_leads": [10],
    },
    "ai": {
        # 总开关。**默认 false**：打开之后消息内容会被发到那个接口（可能是别人
        # 的服务器），所以必须用户自己点头，不能默认替他把内容传出去。
        "enabled": False,
        # 第二个闸门：允许把消息内容上传到外面。两个都得开才真的会发。
        # 分开是为了"我先填好地址，但还没决定要不要真的发出去"这种状态。
        "allow_upload": False,
        # OpenAI 兼容接口。云端（硅基流动/DeepSeek/OpenAI…）和本地（Ollama）
        # 形状一样，都填这里：Ollama 是 http://127.0.0.1:11434/v1
        "base_url": "",
        "api_key": "",
        "model": "",
        # conservative = 只挑明确写了要做的事，不重写不推断；aggressive = 允许归纳重写。
        "mode": "conservative",
        # 短消息不值当问 AI（又慢又花钱），规则解析已经够用。
        "min_chars": 80,
        "timeout_seconds": 60,
        # 后台精修排队的上限：AI 慢的时候堆太多，后面每条都在等它。
        # 超过这个数就"这条先按规则版本用"（任务照常在列表里，只是不精修）。
        "queue_max": 6,
        # 0 = 不限制。
        "token_budget_per_day": 0,
    },
    "resource": {
        "mem_critical_percent": 88,
    },
    "risk": {
        "confirmed_secondary_account": False,
    },
    "wecom_bot": {
        # 群机器人：只要一个 Webhook 地址，不用 CorpID/Secret，也不用配企业可信 IP。
        # 怎么拿：企业微信 → 进一个群（自己建个只有自己的群也行）→ 右上角「…」
        #         → 群机器人 → 添加 → 创建后复制 Webhook 地址，整条贴到下面。
        "enabled": False,
        "webhook": "",                 # ← 用户唯一需要填的一项
        "max_per_minute": 20,          # 官方限制就是 20，改了也没用，留在这里是为了看得见
        "retry_times": 2,              # 网络抖动重试几次
        "timeout": 10,                 # 单次请求超时秒数
        # 推送里要不要带附件（图片原图、文档原件）。手机上看截图靠它。
        "send_attachments": True,
    },
    "push_webhook": {
        # 通用推送地址：钉钉 / 飞书群机器人、Bark 都认这一个框。
        # 不想注册企业微信的人走这条 —— 建个群就有机器人地址，都不用任何"企业"身份。
        #   钉钉：群设置 → 智能群助手 → 添加机器人 → 自定义
        #         安全设置选「自定义关键词」，关键词填「待办」
        #   飞书：群设置 → 群机器人 → 添加机器人 → 自定义机器人
        #   Bark（只有 iPhone）：App 里复制，地址形如 https://api.day.app/你的key
        # 微信里直接收只能靠第三方收费服务，本项目不推荐、也不掺和。
        "enabled": False,              # 和企业微信一样：填了地址还得把这个开关打开
        "url": "",                     # ← 唯一需要填的一项，整条粘进来
        "retry_times": 2,              # 网络抖动重试几次
        "timeout": 10,                 # 单次请求超时秒数
    },
    "qq": {
        # ===== 接入地址 =====
        # NapCat 会主动连到下面这个地址（叫"反向 WebSocket"：由我们监听，
        # 它来连我们）。改了这里，NapCat 那边的配置也要跟着改。
        "enabled": False,                 # 改成 true 才开始收 QQ 消息
        # 要登录哪个 QQ 号。NapCat 是**按号找配置文件**的（onebot11_<QQ>.json），
        # 所以这里必须和实际扫码登录的号一致。界面上的「启动」按钮也靠它。
        "account": 0,
        "bind_host": "127.0.0.1",         # 只监听本机，不接受外面的连接
        "bind_port": 18080,
        "path": "/onebot/v11/ws",
        # 首次启用时程序会自动生成一串随机令牌，照抄到 NapCat 配置里。
        # 它的作用是：万一有别的程序也能连本机端口，没有令牌就进不来。
        "token": "",
        # ===== 收哪些消息（默认一个都不收，必须显式勾选）=====
        # 群里闲聊太多，默认不接收是对的：不勾就一条都不进。
        "group_ids": [],                  # 要监控的群号，例如 123456, 789012（界面上直接写数字）
        "receive_all": False,             # true = 所有群都收（不推荐，噪声和费用都会失控）
        "private_ids": [],                # 要监控的私聊对象 QQ 号
        "receive_private_all": False,
        # 小号收件箱：你自己发给这些会话的消息也会变成待办。
        # 微信/短信截图转发到这里，就等于丢进收件箱。
        "inbox_ids": [],
        "only_mention": False,            # true = 群里只有 @我 的消息才收
        # ===== 开机补收（程序关着那段时间的消息）=====
        # 刚连上时问一次"最近几条历史"，把开机前的一小段补进来。
        # 只能补到 QQ 愿意给的那一小段（通常每个会话最近几十条），
        # 补不了"昨天一整天"；重复的会被自动去掉，不会变成两条待办。
        "catch_up": True,
        "catch_up_count": 20,             # 每个会话最多补几条
        "catch_up_hours": 12,             # 太老的不要（超过这么多小时的不补）
        "catch_up_total": 60,             # 一次总共最多补几条（开 AI 时每条都要过一遍模型）
        # 掉线自救：连着这么多分钟没连上、而 NapCat 进程还活着，就自动带号重启一次
        # （等于用户手点「重新启动 QQ」）。填 0 = 关掉，掉线了就等你自己点。
        # ⚠️ 为什么进程活着也要重启：2026-10-16 那天早上网络抖了一下，登录态掉了，
        # NapCat 退回要二维码、每 2 分钟报一次错，但进程一直没退 —— 用户只能手动点。
        "auto_restart_minutes": 5,
        # 一天最多自动带号重启几次 QQ（0 = 不限）。连上就重新给满额的话，
        # 一天重启 5 次后台就是 15 次登录 —— QQ 侧看到的是"同一个号反复登录"，
        # 那才是真容易被风控盯上的形状。
        "restart_cap_per_day": 6,
        # ===== 安全阀 =====
        "max_text_chars": 3000,           # 单条消息最多留多少字，防超长轰炸
    },
    # 课表插件的设置。插件在 plugins/timetable.py；不用它的话这一段留空就行。
    "timetable": {
        # iCal 订阅地址（.ics）。留空 = 不用课表插件。
        # 学校教务系统里找「导出日历 / 订阅」拿到的那个地址，webcal:// 开头也认。
        "url": "",
        # 或者指一个本地文件：.ics / .csv / .json / .txt 都能读。
        # 格式靠内容自动认，不用告诉程序是哪种。
        "file": "",
        # 往后看几天。默认一周 —— 只把最近的课变成待办，
        # 整学期的课一次性灌进来会把待办列表淹掉。
        "days_ahead": 7,
        # 开学第一周的周一，例如 "2026-09-01"。只有表格里写了
        # "单周/双周/1-16周" 时才需要它；留空就当成每周都上。
        "term_start": "",
    },
    "mail": {
        # 邮箱插件（plugins/mail.py）：只在点「同步」时拉一次，不常驻、不轮询。
        # 留空 = 不用这个插件。
        "enabled": False,
        # 各家邮箱的 IMAP 服务器地址：
        #   QQ 邮箱 imap.qq.com ／ 163 imap.163.com ／ Gmail imap.gmail.com
        #   Outlook outlook.office365.com
        "host": "",
        "port": 993,
        "user": "",                 # 你的邮箱地址
        # ⚠️ 这里要填**授权码**，不是登录密码 —— 邮箱设置里生成的那串。
        # 它存在本地 config.toml 里，别把填好的这份发人、别传网盘。
        "password": "",
        "folder": "INBOX",
        # 第一次同步只看最近几天、最多几封 —— 一次性把两百封邮件灌进待办，
        # 用户第一眼就懵了（而且每封都要过一次 AI）。
        "keep_days": 7,
        "max_per_sync": 10,
    },
    # 界面自己的小限制。
    "ui": {
        # 拖进窗口的文件最大多少 MB。
        # 为什么有上限：拖进来的文件内容是**摊成文本**传给后端的（WebView2
        # 不给网页真实路径），几十 MB 会把窗口卡住。更大的文件走
        # `python run.py --file 路径`。
        "drop_max_mb": 12,
        # 有没有新版本。**默认开**：只发一个 GET（不传任何数据），
        # 失败就安静地算了；不想要就把上面那个开关关掉。
        "update_check": True,
        # 从哪个仓库问。你自己 fork 了就把这里改成你的。
        "update_source": "Landercey/personal-secretary",
        # 多久最多问一次（小时）。0 = 每次启动都问。
        "update_hours": 24,
    },
}

# 新建项目必须零真实信息（CONTRACT.md 第十节）。这里只放占位值。
_PLACEHOLDER_KEYS = {"webhook", "cloud_api_key", "cloud_base_url", "cloud_model", "local_model"}

# 中文输入法下很容易打出全角标点。TOML 里当语法用的标点**全是半角**，
# 所以全角的一定是笔误。
#
# 不纠的后果很吓人（真踩过）：用户在 config.toml 里写
#     group_ids = [123456，789012]
# 于是整份配置解析失败，**所有设置静默退回默认值** ——
# 企业微信 webhook 没了、群号空了、token 没了。
# 用户看到的现象是"我就打了个逗号，怎么设置全丢了"。
#
# 只在**第一次解析失败之后**才用这张表，所以正常文件一个字都不会被动。
FULLWIDTH_FIXES = str.maketrans({
    "，": ",", "：": ":", "＝": "=", "＃": "#",
    "＂": '"', "“": '"', "”": '"',
    "［": "[", "］": "]", "｛": "{", "｝": "}",
})


def _loads_tolerant(raw: str) -> tuple[dict[str, Any] | None, bool]:
    """读 TOML 文本。返回 (结果, 是不是纠过标点)；读不动返回 (None, False)。

    只在第一次解析失败时才纠标点重试一次 —— 为的是一种笔误不要把
    用户整份配置打回默认值。
    """
    try:
        return tomllib.loads(raw), False
    except tomllib.TOMLDecodeError:
        pass
    fixed = raw.translate(FULLWIDTH_FIXES)
    if fixed == raw:
        return None, False
    try:
        return tomllib.loads(fixed), True
    except tomllib.TOMLDecodeError:
        return None, False

# ---------------------------------------------------------------------------
# 生成给用户看的 config.toml 时，每一项上面带一句人话说明。
# 用户唯一会亲手改的就是这个文件，所以说明写在配置里、而不是只写在代码里。
# 加了新配置项记得在这里补一句 —— 补漏了不会报错，只是用户看不懂那一项。
# ---------------------------------------------------------------------------
KEY_COMMENTS: dict[str, str] = {
    # [parse] 理解层怎么判
    "parse.filter_strength": "过滤强度，三档：strict 严格（默认）/ loose 宽松 / off 关闭。\n"
                             "#   觉得丢太多就改 loose；觉得碎片太多就保持 strict。\n"
                             "#   有人说碎片其实是暗号（学号、房间号），那就改 off。",
    "parse.merge_window_seconds": "同一个人在同一个群里，这个秒数内连续发的东西算一件事。",
    "parse.due_default_hour": "只说了哪天、没说几点时，算当天的几点。默认当天最后一分钟。",
    "parse.my_names": "你自己的名字/昵称，直接用逗号分开（例：张三, 小张；不加引号不加中括号）\n"
                      "#   值班表、排班表里含这些词的行会被单独拎出来一条，\n"
                      "#   你不用在整张表里找「我周几值日」。",
    "parse.merge_window_seconds": "同一个人在同一个会话里，这么多秒内连发的东西算一件事。\n"
                                  "#   典型场景：先发一句「明天要交作业」，紧接着发一张图。",
    # [title]
    "title.max_len": "标题最多多少字，超了截断。",
    "title.min_meaningful_chars": "摘掉时间和编号后少于这么多汉字，就当成碎片丢掉。\n"
                                  "#   这是「拿快递」和「（A组）」之间的分界线，\n"
                                  "#   调小了会漏掉真待办，调大了会放进碎片。",
    # [store]
    "store.attachment_retention_days": "图片/文件保留多少天，过期自动删（0 = 永不删）。",
    "store.keep_dropped_days": "废件箱记录保留多少天。这些是调规则的案例，别留太短。",
    # [extract]
    "extract.ocr_engine": "OCR 引擎：auto 自动 / rapid 只用 RapidOCR / windows 只用系统自带。\n"
                          "#   装了 RapidOCR 会明显更准（手写也能读），但要占 390 MB；\n"
                          "#   装法：pip install rapidocr_onnxruntime",
    "extract.max_pages": "PDF 最多处理多少页。整本教材 OCR 会跑到天荒地老。",
    "extract.render_scale": "扫描件 PDF 渲染倍数。调大更清楚，但更慢、更吃内存。",
    "extract.ocr_timeout_seconds": "单张图片识别的超时（秒）。",
    "extract.max_attachment_mb": "单个附件超过多少 MB 就不处理，防止吃光内存。",
    # [push]
    "push.enabled": "推送总开关。关掉之后一条都不会自动推（手动 --push 还能用）。",
    "push.times": "每天推送的时间点，24 小时制，想推几次就写几个。\n"
                  "#   标题按时间点自动定：中午前叫「今天要做的」，傍晚后叫「明天要留意的」。",
    "push.catch_up_minutes": "错过了还补推吗：开机比推送时间晚了多久以内，还补这一次。\n"
                             "#   填 0 = 错过就永远不补。",
    "push.quiet_hours": "免打扰时段：这段时间不发临期提醒（汇总照发），默认 23:00-07:30。\n"
                        "#   格式 23:00-07:30；留空 = 关掉。",
    "push.quote_max_chars": "推送里附带的原文片段长度（方便你看它为什么被判成待办）。",
    "push.fallback_text_chars": "AI 用不了时，推送原文的前多少字作为保底。",
    # [remind] 提醒力度
    "remind.options": "待办详情里「提醒力度」能选的档（分钟），直接写数字、逗号分开（例：60, 30, 10, 3）\n"
                      "#   1440 = 提前一天。想加「提前 5 分钟」就添个 5。\n"
                      "#   不用写 0：下拉里永远有一个「默认」，\n"
                      "#   默认 = 不单独提醒，只在每日汇总里出现（没选的就是这个）。",
    "remind.default_leads": "新待办默认带哪几档提醒，默认 60, 30, 10, 3（全选）\n"
                            "#   留空 [] = 新待办一声不响，只在每日汇总里出现。\n"
                            "#   嫌吵就删几档，比如 [30, 10]；每条还能在详情里单独改。",
    "remind.timetable_leads": "课表单独一套提醒，默认 10：上课前 10 分钟响一次\n"
                              "#   一天好几节课，档位配太多会变成轰炸；不想提醒就留空 []。",
    # [ai]
    "ai.enabled": "打开之后，长消息会交给 AI 挑待办。默认关闭 ——\n"
                  "#   关着的时候走纯规则解析：不联网、不花钱、内容不出去。",
    "ai.allow_upload": "允许把消息内容发到这个接口。默认关闭：\n"
                       "#   地址填好、但还没想好要不要真发出去时，就让它关着。\n"
                       "#   开了之后：消息原文（含群名、附件里读出来的字）会离开这台电脑。",
    "ai.base_url": "OpenAI 兼容的接口地址，填到 /v1 为止。\n"
                   "#   本地 Ollama：http://127.0.0.1:11434/v1（不用 key，随便填一个）\n"
                   "#   云端各家都长一样：https://xxx/v1\n"
                   "#   （也可以直接填完整的 .../chat/completions，两种都认）",
    "ai.api_key": "接口密钥。别把填好的 config.toml 发人、别传网盘。\n"
                  "#   本地 Ollama 随便填个 ollama 就行。",
    "ai.model": "模型名，照服务商的写（比如 qwen2.5:7b、deepseek-chat）。\n"
                "#   免费模型的名字会变，换服务商时要改这一行。",
    "ai.mode": "conservative 保守（只挑明确写了要做的事）/ aggressive 激进（允许重写归纳）。\n"
               "#   默认保守：宁可少收一条，也不要给你编一条出来。",
    "ai.min_chars": "短于这么多字的正文不麻烦 AI（规则解析够用了，AI 又慢又花钱）。",
    "ai.timeout_seconds": "等接口多少秒算失败。超时就退回规则解析，不会卡住收消息。",
    "ai.queue_max": "后台精修最多排几条；再多就这条先按规则版本用（AI 慢时不至于排长队）。",
    "ai.token_budget_per_day": "每天最多用多少 token，0 = 不限制。超了这天就不再用 AI。",
    # [resource]
    "resource.mem_critical_percent": "内存占用超过这个百分比时，只记待办不分析不推送（别在打游戏时抢资源）。",
    # [risk]
    "risk.confirmed_secondary_account": "风控提示确认过没有。程序自己写的，不用管。",
    # [wecom_bot]
    "wecom_bot.enabled": "改成 true 才会真的推送。没配好时推送会打印在屏幕上，不会静默丢消息。",
    "wecom_bot.webhook": "企业微信群的 Webhook 地址，整条贴进来。\n"
                         "#   怎么拿：企业微信 → 进一个群（自己建个只有自己的群也行）\n"
                         "#          → 右上角「…」→ 群机器人 → 添加 → 创建完复制地址。",
    "wecom_bot.max_per_minute": "企业微信的硬限制就是每分钟 20 条，改了没用，放这里是为了看得见。",
    "wecom_bot.retry_times": "网络抖动时重试几次。",
    "wecom_bot.timeout": "单次请求等多久算超时（秒）。",
    "wecom_bot.send_attachments": "推送时要不要连图片/文件原件一起发到手机。嫌手机响得勤就改 false。",
    # [push_webhook] —— 不用企业微信的人填这一段
    "push_webhook.enabled": "改成 true 才会真的推送。两个通道都开着时走企业微信（它能发附件）。",
    "push_webhook.url": "整条地址粘进来就行：企业微信 / 钉钉 / 飞书群机器人、Bark 都认。\n"
                        "#   企业微信的地址（qyapi.weixin.qq.com 开头）填这儿也照样能发，\n"
                        "#   区别只有一个：这里发的**不带附件**，要连图片文件一起发到手机，\n"
                        "#   用上面那段「企业微信机器人」。\n"
                        "怎么拿地址见 README 的「不用企业微信也能收」。",
    "push_webhook.retry_times": "网络抖动时重试几次。",
    "push_webhook.timeout": "单次请求等多久算超时（秒）。",
    # [qq]
    "qq.enabled": "改成 true 才开始收 QQ 消息。改完要重启程序。",
    "qq.account": "要登录哪个 QQ 号（填数字，不要引号）。\n"
                  "#   NapCat 是按号找配置文件的，所以这里必须和你扫码登录的号一致。\n"
                  "#   填了它，设置里那个「启动 QQ」按钮才知道该起哪个号。\n"
                  "#   ⚠️ 这个号**必须先从你电脑上那个 QQ 里退出登录** —— 同一个号\n"
                  "#      不能同时登两个电脑端，不退的话扫码会被拒（手机上那个不算冲突）。",
    "qq.bind_host": "监听地址。默认 127.0.0.1 只接受本机连接，一般不用改。",
    "qq.bind_port": "监听端口。和 NapCat 那边配的端口要一致。",
    "qq.path": "监听路径。和 NapCat 那边配的路径要一致。",
    "qq.token": "访问令牌。首次启用时程序会自动生成一串，照抄到 NapCat 配置里。",
    "qq.group_ids": "要监控的群号，直接写数字，用逗号分开（例：123456, 789012）\n"
                    "#   不要加中括号 —— 那是配置文件里的写法，界面上直接写数字就行\n"
                    "#   不填 = 一个群都不收",
    "qq.receive_all": "true = 所有群都收。不推荐：闲聊噪声多，接 AI 的话费用也会失控。",
    "qq.private_ids": "要监控的私聊对象 QQ 号，写法和上面一样（数字，逗号分开，不加中括号）",
    "qq.receive_private_all": "true = 所有私聊都收。",
    "qq.inbox_ids": "小号收件箱：你自己发给这些会话的消息也变成待办。\n"
                    "#   把微信/短信截图转发到这个会话，就等于丢进收件箱。\n"
                    "#   ⚠️ 收件箱里的东西**全盘接收**：不过滤闲聊、不过滤碎片，\n"
                    "#      连文件里读出来的字也当待办（别的地方会严格过滤）。\n"
                    "#      因为那是你专门记给自己的，机器不该替你判断值不值得。",
    "qq.only_mention": "true = 群里只有 @我 的消息才收。群太吵可以打开。",
    "qq.catch_up": "刚连上时，把「开机前那一小段」的消息补进来（默认开）。\n"
                   "#   只能补到 QQ 愿意给的一小段（通常每个会话最近几十条），\n"
                   "#   补不了「昨天一整天」那种；重复的会被自动去掉。",
    "qq.catch_up_count": "每个会话最多补几条，默认 20。",
    "qq.catch_up_hours": "只补最近多少小时内的消息，默认 12。更老的不补 —— "
                         "一晚上的刷屏全灌进来只会把待办列表淹掉。",
    "qq.catch_up_total": "一次总共最多补几条，默认 60。\n"
                         "#   开了 AI 的话每条都要过一次模型，别设太大（开机时会卡）。",
    "qq.auto_restart_minutes": "连着这么多分钟没连上（而 NapCat 进程还活着）就自动\n"
                               "#   带号重启一次，重试快速登录（等于你手点「重新启动 QQ」）。\n"
                               "#   默认 5 分钟；填 0 = 关掉，掉线了就等你自己点。",
    "qq.restart_cap_per_day": "一天最多自动重连几次 QQ，默认 6，填 0 = 不限。\n"
                              "#   用完就不自动重启了 —— 反复登录才会被风控盯上。",
    "qq.max_text_chars": "单条消息最多保留多少字，防超长内容刷屏。",
    # [timetable] 课表插件
    "timetable.url": "课表订阅地址（iCal / .ics）。留空 = 不用这个。\n"
                     "#   手机课表 App（WakeUp / 小爱 / 超级课程表）里找「导出 / 分享」，\n"
                     "#   学校教务系统里找「导出日历 / 订阅」，把地址整条贴进来。\n"
                     "#   填好后跑一次 python run.py --sync，课就会变成待办。",
    "timetable.file": "或者指一个本地课表文件，.ics / .csv / .json / .txt 都能读。\n"
                      "#   格式靠内容自动认，不用告诉程序是哪种。\n"
                      "#   ⚠️ Excel（.xlsx）要先「另存为 → CSV UTF-8」，程序不直接读 Excel。",
    "timetable.days_ahead": "往后看几天。默认 7 天，只把最近的课变成待办。",
    "timetable.term_start": "开学第一周的周一，例如 \"2026-09-01\"。\n"
                            "#   只有表格里写了「单周 / 双周 / 1-16周」才需要填；\n"
                            "#   留空就当成每周都上（一周内的待办不受影响）。",
    # [mail] 邮箱插件
    "mail.enabled": "打开之后，点「同步」会去邮箱拉最近的通知，变成待办。\n"
                    "#   只在点同步时拉一次：不常驻连接、不轮询、不标已读。",
    "mail.host": "邮箱的 IMAP 服务器地址。\n"
                 "#   QQ 邮箱 imap.qq.com ／ 163 imap.163.com ／ Gmail imap.gmail.com\n"
                 "#   Outlook outlook.office365.com（拿不准就搜「<你的邮箱> IMAP 服务器」）",
    "mail.user": "你的邮箱地址（要收信的那个）。",
    "mail.password": "⚠️ 这里填**授权码**，不是你的登录密码。\n"
                     "#   QQ 邮箱：设置 → 账号 → 开启 IMAP → 生成授权码\n"
                     "#   163 邮箱：设置 → POP3/SMTP/IMAP → 开启 → 新增授权密码\n"
                     "#   Gmail：开两步验证后生成「应用专用密码」\n"
                     "# 它明文存在本地 config.toml 里：别把这份文件发给别人、别传网盘。",
    "mail.port": "IMAP 端口，默认 993（SSL）。没特殊要求不用改。",
    "mail.folder": "看哪个文件夹，默认收件箱 INBOX。",
    "mail.keep_days": "第一次同步只看最近几天的邮件，默认 7 天。\n"
                      "#   往后每次同步只拉「上次之后的新邮件」，不再看这个。",
    "mail.max_per_sync": "一次最多拉几封，默认 10。\n"
                         "#   别设太大：每封都会过一次识别（开了 AI 就过一次 AI）。",
    # [ui] 界面
    "ui.drop_max_mb": "拖进窗口的文件最大多少 MB，默认 12。\n"
                      "#   拖进来的文件是「摊成文本」传给后端的（网页拿不到真实路径），\n"
                      "#   太大（几十 MB）会把窗口卡住。更大的文件用 --file 命令。",
    "ui.update_check": "启动时问一下 GitHub 有没有新版本。\n"
                       "#   只是发一个 GET，**不传任何你的数据**；问不到就当没有，不影响使用。\n"
                       "#   有新版会在左下角出现一行，点一下就跳到下载页。",
    "ui.update_source": "从哪个仓库问新版本（用户名/仓库名）。你 fork 一份改成你自己的。",
    "ui.update_hours": "多久最多问一次（小时），默认 24。填 0 = 每次启动都问。",
}


# --------------------------------------------------------------------------
# 界面上「设置」面板长什么样：先分四组（标签页），组里再分段。
#
# 为什么非得分组：之前 11 段平铺成一长条，用户的原话是"太杂，没有分类" ——
# 想改推送时间得从"企业微信"往下翻过 QQ、课表、AI、文件解析……
# 分组之后每个标签页只放 2~3 段。
#
# 这里只排顺序和起名，**每一项的说明直接用 KEY_COMMENTS** ——
# 配置文件和界面共用同一句话，不会出现"文件里写了、界面上没写"的漂移。
# 加了新配置段记得在这里补一行，补漏了不会报错，只是那段排到最后。
# --------------------------------------------------------------------------
SECTION_GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    ("group_push", "推送到手机", "待办怎么发到你手机上", (
        ("wecom_bot", "企业微信机器人", "不想注册企业微信？看下面「通用推送地址」"),
        ("push_webhook", "通用推送地址", "企业微信 / 钉钉 / 飞书 / Bark，填一个就行"),
        ("push", "什么时候推", "一天推几次、错过了补不补"),
        ("remind", "提醒强度", "哪些事要在截止前单独提醒一次"),
    )),
    ("group_receive", "收消息", "消息从哪儿来、用哪个号挂着收", (
        ("qq", "QQ 接入", "收哪些群、哪些私聊"),
        ("risk", "用大号还是小号", "挂机之前先想清楚"),
        ("timetable", "课表", "把课表变成待办（不用就留空）"),
        ("mail", "邮箱", "把邮件通知也变成待办（可选，点同步才拉）"),
    )),
    ("group_understand", "看懂消息", "什么样的消息算待办", (
        ("ai", "AI 增强", "可选。不填就只用规则解析，功能照常"),
        ("parse", "识别与过滤", "偏严还是偏松、你自己的名字"),
        ("extract", "文件解析", "图片 / PDF / Office 怎么读"),
    )),
    ("group_data", "数据与资源", "存在哪、留多久、别跟你抢资源", (
        ("store", "数据保留", "附件和废件箱留多久"),
        ("resource", "资源占用", "打游戏时别抢资源"),
        ("ui", "界面与更新", "拖文件的限制、有没有新版本"),
        ("title", "标题规则", "很少需要动"),
    )),
)

# 一般不用动的东西。界面上折进每段底部的「高级」里，默认收起 ——
# 一屏塞 60 个框，用户不知道哪个能碰、哪个碰了会出事。
# 插件声明的设置段：section -> (标题, 说明)。由 plugins.py 在加载插件时填，
# `schema()` 把它们单独归到「插件」组里 —— 插件作者不用碰前端就能有自己的设置页。
PLUGIN_SECTIONS: dict[str, tuple[str, str]] = {}


def register_plugin_section(section: str, title: str = "", desc: str = "",
                            defaults: dict[str, Any] | None = None,
                            comments: dict[str, str] | None = None) -> None:
    """插件注册一段自己的设置（由插件清单里的 `settings` 触发）。

    注册进 DEFAULTS 之后，`config.set()` 才认得这些键（不然会 KeyError），
    界面也会自动把它画出来 —— 插件作者不需要改一行前端代码。
    """
    section = str(section or "").strip()
    if not section:
        return
    DEFAULTS.setdefault(section, {}).update(defaults or {})
    for key, text in (comments or {}).items():
        KEY_COMMENTS.setdefault(f"{section}.{key}", str(text))
    PLUGIN_SECTIONS[section] = (str(title or section), str(desc or ""))


ADVANCED_KEYS: frozenset[str] = frozenset({
    "qq.max_text_chars",
    # 监听地址 / 端口 / 路径 / 令牌：程序自己写进 NapCat 的配置，单方面改了两边
    # 就对不上（QQ 会静默收不到消息）。小白模式干脆不显示 —— 用户的原话是
    # "还有一些监听接口啥的怕被用户弄坏"。
    "qq.bind_host", "qq.bind_port", "qq.path", "qq.token",
    "parse.merge_window_seconds", "parse.due_default_hour",
    "extract.ocr_engine", "extract.max_pages", "extract.render_scale",
    # AI 这两项一般不用动：短于多少字才问 AI、等多久算超时。
    "ai.token_budget_per_day", "ai.min_chars", "ai.timeout_seconds", "ai.queue_max",
    # 邮箱这四项一般不用动：端口、看哪个文件夹、第一次看几天、一次拉几封。
    "mail.port", "mail.folder", "mail.keep_days", "mail.max_per_sync",
    # 补收的三个上限一般不用动（补几条、多久内、一次总共几条）。
    "qq.catch_up_count", "qq.catch_up_hours", "qq.catch_up_total",
    # 掉线自救的等待时长（默认 5 分钟）也不用动。
    "qq.auto_restart_minutes", "qq.restart_cap_per_day",
    "title.max_len", "title.min_meaningful_chars",
    # 「软件更新」只留一个"自动检查"开关在外面；来源、频率、拖拽上限是调参。
    "ui.update_source", "ui.update_hours", "ui.drop_max_mb",
    # 废件箱留多久：不填也能用（默认 30 天），属调参。
    "store.keep_dropped_days",
})

# 整段都是"调参"的：小白模式下直接不显示（设置 → 右上角「高级」打开才出现）。
# 判据只有一条：**不填也不影响第一次用**。数据保留（store）不算 —— 那里挂着
# 备份和恢复的按钮，藏了等于把"数据能救回来"这件事藏了。
ADVANCED_SECTIONS: frozenset[str] = frozenset({
    "title",        # 标题截多长、最短几个字才算待办
    "extract",      # OCR 引擎、PDF 最多几页、渲染倍率
    "resource",     # 内存超过多少就不干活
    # ui 不整段藏：用户要能一眼找到"有没有新版本"，只把它的三个调参项归到高级。
    "mail",         # 邮箱插件那一堆连接参数
})

# 由**程序自己管**的项：程序会把同样的值写进 NapCat 的配置文件里。
# 用户在这边单方面改，两边就对不上，QQ 会**静默收不到消息** ——
# 这是最讨厌的那种坏法（界面看着一切正常，就是没反应）。
# 所以默认锁住，界面上要明确点一下「我要改」才能动。
LOCKED_KEYS: dict[str, str] = {
    "qq.bind_host": "程序只监听本机（127.0.0.1）。改成别的地址等于把这个端口"
                    "开给局域网，别人也能连进来。",
    "qq.bind_port": "程序会把同一个端口写进 NapCat 的配置。你在这儿单方面改，"
                    "NapCat 就再也连不上来了。",
    "qq.path": "同上 —— 必须和 NapCat 那边配的路径一模一样。",
    "qq.token": "程序自动生成，并写进 NapCat 的配置。在这儿改，"
                "要重新装一次 NapCat 才能对上。",
}

# **改完要重启才生效**的项。
#
# 为什么值得单独列出来：用户在界面上改完、点保存、弹一句"已保存"，
# 然后以为立刻生效了 —— 结果没反应，只能来问作者或者骂程序。
# （用户原话："要重启的提醒力度小"。）
# 界面拿到这个标记之后，保存完会**在保存条那里竖一条明显的警告**，
# 并给一个「现在重启后台」的按钮。
RESTART_KEYS: frozenset[str] = frozenset({
    # 端口/地址在接收端启动时就绑好了，改完不重启等于没改。
    "qq.enabled", "qq.bind_host", "qq.bind_port", "qq.token", "qq.path",
    "qq.account",
    # ⚠️ 下面这几个也必须重启（2026-10-13 真踩过）：收哪些会话是后台**启动时**
    # 读一次存进 `QQSettings` 的，之后改配置文件它不会重读。
    # 以前它们不在这个名单里 → 界面上不提醒 → 用户打开"收所有私聊"、
    # 保存、然后发现发消息过去毫无反应（消息其实到了，全被按老设置拦掉了）。
    "qq.group_ids", "qq.private_ids", "qq.inbox_ids",
    "qq.receive_all", "qq.receive_private_all", "qq.only_mention",
})

def _kind(value: Any) -> str:
    """这个配置项在界面上用什么控件。

    类型直接从默认值推 —— 不用再维护一张"哪个键是哪种类型"的表，
    加了新配置项界面自动就对。
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, list):
        return "list"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "text"


def coerce(section: str, key: str, value: Any) -> Any:
    """界面传回来的值（基本是字符串）转成配置该有的类型。

    为什么必须转：TOML 里 `[123, 456]` 和 `["123", "456"]` 是两回事，
    后者会让"群号比对"永远不相等 —— 表现为"我明明填了群，一条都收不到"。
    """
    kind = _kind(DEFAULTS[section][key])
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "开")
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    if kind == "list":
        items = value if isinstance(value, list) else re.split(r"[,，\n]", str(value))
        items = [str(item).strip() for item in items if str(item).strip()]
        # 全是数字就当数字（群号、QQ 号）；否则当字符串（时间点、名字）
        if items and all(item.isdigit() for item in items):
            return [int(item) for item in items]
        return items
    return str(value)

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return str(value)


def render_toml(values: dict[str, dict[str, Any]]) -> str:
    """把配置渲染成带逐项说明的 TOML 文本。

    说明来自 KEY_COMMENTS。用户会亲手改这个文件，所以每一项上面都要有
    "这是什么、改了会怎样"，而不是一串看不懂的键名。
    """
    lines = [
        "# 私人秘书 · 用户配置",
        "# 这里只放这台机器自己的设置，不要提交到任何仓库、不要发给别人。",
        "#",
        "# 改完保存就行 —— 绝大多数设置立即生效，不用重启。",
        "# 例外：[qq] 那段改动后要重启（端口在启动时就绑好了）。",
        "",
    ]
    for section in sorted(values):
        lines.append(f"# {'=' * 66}")
        lines.append(f"# [{section}]")
        lines.append(f"# {'=' * 66}")
        lines.append(f"[{section}]")
        for key in sorted(values[section]):
            comment = KEY_COMMENTS.get(f"{section}.{key}")
            if comment:
                for piece in comment.split("\n"):
                    lines.append(piece if piece.lstrip().startswith("#") else f"# {piece}")
            lines.append(f"{key} = {_toml_value(values[section][key])}")
        lines.append("")
    return "\n".join(lines)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for section, values in (override or {}).items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section].update(values)
        else:
            out[section] = values
    return out


def _trim_period(text: str) -> str:
    """界面上的一句话结尾不要句号 —— 满屏句号读着像公文，用户提过。"""
    return re.sub(r"。(?=\s*$)", "", str(text or "").rstrip())


class Config:
    """读配置。`cfg["parse"]["filter_strength"]` 或 `cfg.get("parse", "filter_strength")`。

    会自动盯着配置文件：每次读取前看一眼文件有没有被改过（最多每 2 秒查一次），
    改了立刻用新的。用户在编辑器里改完保存就能生效，不用重启程序。

    ⚠️ 唯一的例外是 `[qq]` 段：那些值在服务启动时就用来绑端口了，
    改了必须重启。config.toml 的抬头里写明了这一点。
    """

    def __init__(self, values: dict[str, Any] | None = None, path: Path | None = None) -> None:
        self._values = _merge(DEFAULTS, values or {})
        self.path = path
        self._mtime = self._read_mtime()
        self._last_check = 0.0
        # 配置里有全角标点、程序自己纠过：说一声，别让它变成神秘现象
        self.load_note = ""

    # ------------------------------------------------------------------ 热重载
    def _read_mtime(self) -> float:
        if not self.path:
            return 0.0
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        """每 2 秒最多查一次文件时间，变了就重读。

        不做文件监听（那要引第三方库），轮询足够 ——
        用户改完配置到生效最多差 2 秒，察觉不到。
        """
        if not self.path:
            return
        now = time.monotonic()
        if now - self._last_check < 2.0:
            return
        self._last_check = now
        mtime = self._read_mtime()
        if not mtime or mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            # 用户改到一半、语法还没写对时会读失败。保留旧值，等他写完再说 ——
            # 不能因为一次读失败就把正在运行的程序搞乱。
            return
        fresh, fixed = _loads_tolerant(raw)
        if fresh is None:
            return                      # 还是读不动，保留旧值
        self.load_note = ("配置里有中文输入法的全角标点，已经自动按半角读了"
                          if fixed else "")
        self._values = _merge(DEFAULTS, fresh)

    # ------------------------------------------------------------------ 读取
    def __getitem__(self, section: str) -> dict[str, Any]:
        self._maybe_reload()
        return self._values.get(section, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        self._maybe_reload()
        return self._values.get(section, {}).get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {k: dict(v) for k, v in self._values.items()}

    def restart_fingerprint(self) -> str:
        """把"要重启才生效"的那几项的值算成一个指纹。

        后台启动时记一份（见 heartbeat.mark_start），界面拿现在这份比 ——
        不一样就说明**确实**要重启一次。

        ⚠️ 为什么不用配置文件的时间戳当判据（2026-10-13 用户报"他说重启生效，
        但每次重启完下次再打开他还在"）：改推送时间、改过滤强度也会动那个时间戳，
        而那些项是立刻生效的。拿时间戳当判据 = 界面天天喊"要重启"，
        喊到用户不信为止。只比**真的需要重启**的那几项才对得上号。
        """
        import hashlib
        import json

        values = {}
        for full in sorted(RESTART_KEYS):
            section, _, key = full.partition(".")
            values[full] = self.get(section, key)
        blob = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 界面
    def schema(self) -> list[dict[str, Any]]:
        """给设置界面用的清单：分组 → 段 → 项。

        直接从 DEFAULTS + KEY_COMMENTS 生成，所以不存在"界面漏了一项"
        —— 加了新配置项，界面上自动就出现。

        每一项都带 `level`（common / advanced）和 `locked`（不为空就是被锁住，
        值是"为什么锁"）。界面靠这两个字段决定折叠和禁用。
        """
        groups: list[dict[str, Any]] = []
        placed: set[str] = set()

        for group_id, group_title, group_desc, sections in SECTION_GROUPS:
            items = []
            for section, title, desc in sections:
                if section not in DEFAULTS or section in placed:
                    continue
                placed.add(section)
                items.append(self._section_entry(section, title, desc))
            if items:
                groups.append({"name": group_id, "title": group_title,
                               "desc": group_desc, "sections": items})

        # 插件自己声明的设置段：单独一组放在后面（见 register_plugin_section）。
        plugin_items = [self._section_entry(section, title, desc)
                        for section, (title, desc) in PLUGIN_SECTIONS.items()
                        if section in DEFAULTS]
        placed.update(PLUGIN_SECTIONS)
        if plugin_items:
            groups.append({"name": "group_plugins", "title": "插件",
                           "desc": "装了什么插件，它自己的设置就出现在这里",
                           "sections": plugin_items})

        # 兜底：没在 SECTION_GROUPS 里登记过的段，塞进最后一个组。
        # 为什么要兜底：以后加了新配置段忘了登记，用户也得能在界面上改到它。
        rest = [name for name in sorted(DEFAULTS) if name not in placed]
        if rest:
            items = [self._section_entry(name, name, "") for name in rest]
            if groups:
                groups[-1]["sections"].extend(items)
            else:
                groups.append({"name": "group_rest", "title": "其它",
                               "desc": "", "sections": items})
        return groups

    def _section_entry(self, section: str, title: str, desc: str) -> dict[str, Any]:
        keys = []
        # **按 DEFAULTS 里的书写顺序**，不按字母排。那个顺序是照着"用户填的
        # 先后"手写出来的（总开关 → 地址 → 密钥 → 模型），字母序会把
        # 「总开关」排到第四个去 —— 用户从上往下填，第一个框不该是"密钥"。
        for key in DEFAULTS[section]:
            full = f"{section}.{key}"
            keys.append({
                "key": key,
                "value": self.get(section, key),
                "kind": _kind(DEFAULTS[section][key]),
                "hint": _trim_period(KEY_COMMENTS.get(full, "")),
                "level": "advanced" if full in ADVANCED_KEYS else "common",
                "locked": LOCKED_KEYS.get(full, ""),
                # 改完要重启才生效的项：界面保存后会竖一条明显的提醒
                "restart": full in RESTART_KEYS,
            })
        # 常用的排前面，高级的排后面 —— 界面上高级那几项要单独折叠起来
        keys.sort(key=lambda item: item["level"] == "advanced")
        return {"name": section, "title": title, "desc": desc, "keys": keys,
                # 整段都是调参的：小白模式下连标题都不显示
                "advanced": section in ADVANCED_SECTIONS}

    def set(self, section: str, key: str, value: Any) -> None:
        """改一项配置，并写回 config.toml。

        写回是**一行一行改**的，不是整个文件重新生成 ——
        用户可能自己在里面加过注释，整个重写会把那些全冲掉。
        段不存在就补段，键不存在就补键（补在那一段的末尾）。
        """
        if section not in DEFAULTS or key not in DEFAULTS[section]:
            raise KeyError(f"没有这一项配置：{section}.{key}")
        value = coerce(section, key, value)
        self._values.setdefault(section, {})[key] = value
        if not self.path:
            return

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return

        lines = text.splitlines()
        header = f"[{section}]"
        line_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
        new_line = f"{key} = {_toml_value(value)}"

        start = next((i for i, raw in enumerate(lines)
                      if raw.strip() == header), None)
        if start is None:
            lines += ["", header, new_line]
        else:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].lstrip().startswith("[")), len(lines))
            hit = next((i for i in range(start + 1, end) if line_re.match(lines[i])),
                       None)
            if hit is not None:
                lines[hit] = new_line
            else:
                while end > start + 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, new_line)

        try:
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return
        # 自己刚写的那一下不该被当成"用户改了文件"，否则会白白重读一遍
        self._mtime = self._read_mtime()
        self._last_check = time.monotonic()

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """从文件加载；文件不存在就用默认值（并生成一份带注释的模板）。"""
        target = Path(path) if path else default_config_path()
        values: dict[str, Any] = {}
        if target.is_file():
            try:
                raw = target.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            loaded, fixed = _loads_tolerant(raw)
            if loaded is None:
                # 配置写坏了不能把程序带崩：退回默认值，调用方可以通过
                # cfg.load_error 知道自己写错了。
                values = {}
                cfg = cls(values, target)
                cfg.load_error = f"配置文件读不了（{target.name}），已改用默认值"
                return cfg
            values = loaded
            cfg = cls(values, target)
            cfg.load_note = ("配置里有中文输入法的全角标点，已经自动按半角读了"
                             if fixed else "")
            cfg.load_error = ""
            return cfg
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_toml(DEFAULTS), encoding="utf-8")
            except OSError:
                pass
        cfg = cls(values, target)
        cfg.load_error = ""
        return cfg

    load_error: str = ""
    # 配置里有全角标点、程序自己纠过。不是错误，但要说一声（见 FULLWIDTH_FIXES）
    load_note: str = ""

    def is_placeholder(self, section: str, key: str) -> bool:
        """这个值还是不是占位/空值（用来判断"用户还没配置"）。"""
        if key not in _PLACEHOLDER_KEYS:
            return False
        return not str(self.get(section, key, "") or "").strip()


def default_root() -> Path:
    """程序自己的目录（绿色版：就在程序旁边）。"""
    override = os.environ.get("SECRETARY_HOME")
    if override:
        return Path(override)
    # 打包成 exe 之后 `__file__` 指向 `_internal` 里面（那是 PyInstaller 的
    # 私有目录），把 data/config.toml/plugins 放进去，用户既看不见也找不着。
    # exe 旁边的那个文件夹才是"程序自己的目录"。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return default_root() / "config.toml"


def default_data_dir() -> Path:
    return default_root() / "data"


def attachments_dir() -> Path:
    """附件落在哪：`data/attachments/`。

    以前这个路径在三处各写了一遍（QQ 来源、界面拖拽、存储清理）——
    清理那次差点写成别的目录，那就是"该删的没删、不该删的删了"。
    """
    return default_data_dir() / "attachments"


# 全局默认实例。业务代码 import 这个用，别自己 new。
config = Config.load()
