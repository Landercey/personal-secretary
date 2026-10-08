"""第三段：存储。只读写数据库，不放业务规则。

业务规则（什么样的行算待办、怎么去重）全在 pipeline 里；
这里只回答"怎么存、怎么查"。
"""

from .repository import SqliteStore, dedup_key_for, open_database

__all__ = ["SqliteStore", "open_database", "dedup_key_for"]
