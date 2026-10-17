"""自动重连的"每日额度"：一天最多带号重启几次 QQ。

为什么要这个：用户定的红线是"绝对不能被风控"。原来的上限是"每段掉线最多 3 次"，
**连上就重新给满额** —— 一天重启 5 次后台（更新、改配置、开机），就是最多 15 次登录。
QQ 侧看到的是"同一个号一天里反复异地/反复登录"，那才是真会被盯上的形状。

额度按天记在 meta 里（进程重启用同一个库，所以跨重启也算数）。
用完了就不再自动重启，明确写一句话让用户自己去看一眼手机上的登录提示。
"""

from __future__ import annotations

from datetime import datetime


def _key() -> str:
    return f"qq_restart:{datetime.now().strftime('%Y-%m-%d')}"


def used(store) -> int:
    try:
        return int(store.get_meta(_key(), "0") or 0)
    except (TypeError, ValueError):
        return 0


def allow(store, cap: int) -> bool:
    """今天还能不能再自动重启一次。cap <= 0 表示不限（老行为）。"""
    if cap <= 0:
        return True
    return used(store) < cap


def note(store) -> int:
    """记一次，返回今天一共用了几次。"""
    count = used(store) + 1
    try:
        store.set_meta(_key(), str(count))
    except Exception:
        pass
    return count
