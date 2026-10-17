"""错误词典：**编号 → 人话**。

为什么要单独一份：报错文案散在十几处，改一次要翻全项目；而且同一个问题
（比如端口被占）在日志、弹框、反馈包里说法还不一样。这里做两件事：
  · 代码里只写编号（`QQ.BIND.PORT_BUSY`）——稳定、能 grep、能当 Issue 标题；
  · 给人看的话全在这张表里，改一处全项目生效。

新加一个错误：先在 ERRORS 里加一条，再用 `logger.note("error", "你的编号", ...)`。
`tests/test_diagnostics.py` 会检查每条都有三样东西（是什么/为什么/怎么办）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Explain:
    """一条错误的"人话"版本。"""

    what: str       # 发生了什么
    why: str        # 多半是什么原因
    fix: str        # 该怎么办


ERRORS: dict[str, Explain] = {
    "QQ.BIND.PORT_BUSY": Explain(
        "收消息的端口被占用了",
        "多半是另开了一份程序，或者上次的进程没退干净",
        "关掉那一份程序（任务管理器里搜「私人秘书」）再点启动"),
    "QQ.LOGIN.RESTART_CAP": Explain(
        "今天自动重连的次数用完了",
        "登录态反复掉 —— 可能是手机端在别处登录了，或者网络不稳",
        "看一眼手机 QQ 有没有登录提示，手动扫码一次；别一直自动重试"),
    "QQ.CATCH_UP.FAILED": Explain(
        "开机补收消息没成功",
        "NapCat 那边可能还没准备好，或者历史消息已经取不到了",
        "不影响以后的消息；急着找旧消息可以去群里翻"),
    "AI.HTTP.401": Explain(
        "AI 接口说密钥不对",
        "API key 抄错、过期，或者服务商换了密钥",
        "在 设置 → AI 增强 里重新贴一次 key，点「试一下」验证"),
    "AI.HTTP.429": Explain(
        "AI 接口限流了",
        "免费额度用完/请求太密，服务商暂时不让调",
        "过几分钟再试；这段时间长消息会先用规则版，不影响收消息"),
    "AI.CONNECT.FAILED": Explain(
        "连不上 AI 接口",
        "地址写错、本地服务没开、或者代理没开",
        "点 设置 → AI 增强 的「试一下」，它会告诉你具体卡在哪"),
    "PLUGIN.LOAD.FAILED": Explain(
        "有个插件没加载起来",
        "插件文件坏了、依赖没装，或者版本和主程序对不上",
        "主程序和别的插件照常跑；把出问题的那个插件移出 plugins/ 就好"),
    "PLUGIN.RUN.FAILED": Explain(
        "插件在处理某条消息时出错了",
        "插件自己的 bug（主程序已经把这条记下来了）",
        "那条消息会按其他插件/规则继续处理，不影响别的消息"),
    "PUSH.DELIVER.FAILED": Explain(
        "推送没发出去",
        "地址/token 不对、被限流，或者网络不通",
        "去 设置 → 推送到手机 点「发一条测试推送」，会显示对方的原始报错"),
    "PREFLIGHT.DB.CORRUPT": Explain(
        "待办库读坏了",
        "多半是断电/强杀时正好在写库",
        "坏文件已另存；去 设置 → 数据保留 选一份备份恢复"),
    "PREFLIGHT.DISK.LOW": Explain(
        "磁盘快满了",
        "附件、备份、日志都要占空间",
        "清一点空间，至少留 200MB"),
    "PREFLIGHT.DATA.READONLY": Explain(
        "数据目录写不进去",
        "文件夹被设成只读、被杀软锁住，或者放在没有权限的位置",
        "换个文件夹重新解压，或者给 data 目录写权限"),
    "CONFIG.LOAD.FAILED": Explain(
        "配置文件读坏了，已改用默认值",
        "多半是手改 config.toml 时少了个引号/括号",
        "设置里重新填一遍；原文件还在，可以自己比对"),
    "APP.SAFE_MODE": Explain(
        "这次是安全模式启动",
        "上次没正常启动完成（连续三次）",
        "配置和待办都在；改完设置重开一次就恢复正常"),
    "APP.CRASH.UNHANDLED": Explain(
        "程序遇到没预料到的错误，退出了",
        "多半是某个边界情况没处理到",
        "把诊断包发给作者（设置 → 意见反馈 → 保存诊断包）"),
    "OCR.FAILED": Explain(
        "图片里的字没读出来",
        "图片太糊、是手写，或者系统 OCR 组件缺失",
        "把图原样留在 QQ 里；也可以把关键内容打成文字发过来"),
    "EXTRACT.FAILED": Explain(
        "这个文件没能读成文字",
        "格式太老（.doc/.xls）、文件损坏，或者加密了",
        "用 Office 另存为 .docx/.xlsx 再发一次"),
    "UPDATE.DOWNLOAD.FAILED": Explain(
        "升级包没下载成功",
        "网络断了、代理没开，或者临时下到一半",
        "会自动重试 3 次；还不行就手动下 upgrade.exe 双击"),
}


def explain(code: str) -> Explain | None:
    return ERRORS.get(str(code or "").strip())


def human(code: str, detail: str = "") -> str:
    """给人看的一句话（界面/弹框用）。没有编号就退回通用说法。"""
    item = explain(code)
    if item is None:
        return f"{detail or '出了点问题'}（编号 {code}）"
    text = item.what
    if item.fix:
        text += f"\n怎么办：{item.fix}"
    if detail:
        text += f"\n技术细节：{detail}"
    return text
