"""AI 精修：接收端只跑规则，AI 在**后台线程**里补。

为什么要这个（2026-10-17 实测）：
  · 规则 200 条 0.6 秒入库；但只要有一条走了 AI，后面的消息就得等它 ——
    AI 超时默认 60 秒，整个接收端跟着停 60 秒。
  · 用户要的是"先看到、后改好"：规则那版立刻入库可见，AI 回来再把标题修准。

三条硬规矩（少一条就会出乱子）：
  1. **只在后台线程里问模型**：接收循环一秒都不能被它占住。
  2. **改之前核对**：这条还得开着、标题还是插入时那个（用户没手动改过）——
     见 `SqliteStore.refine_task`，检查和改写是同一把锁里做的。
  3. **排队太多就熔断**：AI 本来就慢，堆十条等于全都在等；超过 `ai.queue_max`
     就这条先按规则版本用（任务照常在列表里，只是不精修）。

精修只改标题/时间/地点，**不动**用户的东西（提醒档、附件、状态）。
"""

from __future__ import annotations

import queue
import threading

from .config import config


class Refiner:
    """一个后台工作线程 + 一个队列。FIFO，保证同一批消息的处理顺序不乱。"""

    def __init__(self, store, enricher, on_event=None) -> None:
        self.store = store
        self.enricher = enricher
        self.on_event = on_event
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopped = threading.Event()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "Refiner":
        if self._worker is None or not self._worker.is_alive():
            self._stopped.clear()
            self._worker = threading.Thread(target=self._loop, name="ai-refine",
                                            daemon=True)
            self._worker.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        """收工。队列里没处理完的就不做了 —— 它们已经有规则那版在列表里。"""
        self._stopped.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        self._worker = None

    # ------------------------------------------------------------------ 投递
    def submit(self, message, tasks) -> bool:
        """把一条刚入库的消息交给后台。返回"接没接"（没接说明规则那版就够了）。"""
        if self.enricher is None:
            return False
        try:
            if not self.enricher.enabled():
                return False
            text = str(getattr(message, "text", "") or "")
            if not text.strip() or not self.enricher.wants(text):
                return False          # 短消息：规则够用，别花这个钱也别占这条队
            cap = int(config.get("ai", "queue_max", 6) or 6)
            if self._queue.qsize() >= max(1, cap):
                self._say(f"AI 排队超过 {cap} 条，这条先用规则版本")
                return False
        except Exception as exc:      # 插件自己坏了也不该影响收消息
            self._say(f"AI 精修不可用（{type(exc).__name__}）")
            return False

        pairs = [(int(t.task_id), str(t.title)) for t in tasks or []]
        if pairs:
            self.store.mark_ai_pending([task_id for task_id, _title in pairs])
        self._queue.put((message, pairs, text))
        self.start()
        return True

    # ------------------------------------------------------------------ 干活
    def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                message, pairs, text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(message, pairs, text)
            except Exception as exc:      # 后台线程绝不能死
                self._say(f"AI 精修出错：{type(exc).__name__}: {exc}")
                self.store.mark_ai_pending([task_id for task_id, _t in pairs], False)
            else:
                self.store.mark_ai_pending([task_id for task_id, _t in pairs], False)
            finally:
                self._queue.task_done()

    def _handle(self, message, pairs, text: str) -> None:
        try:
            answer = self.enricher.answer(text)
        except Exception as exc:
            self._say(f"AI 连不上，这条先用规则版本（{type(exc).__name__}）")
            if not pairs:
                # 规则一条都没出、AI 又没连上：按契约给一条"看一眼这条"，
                # 别让一条长消息凭空消失（以前这是 AI 插件同步做的）。
                self.store.save_drafts(message, [self.enricher.fallback(message, text)])
            return

        drafts = self.enricher.drafts_from(answer, message, text)
        if not drafts:
            return                    # 模型说"没有要做的事"：保留规则那版

        refined = 0
        for (task_id, expect_title), draft in zip(pairs, drafts):
            if self.store.refine_task(
                    task_id, title=draft.title, expect_title=expect_title,
                    due_at=draft.due_at, location=draft.location,
                    quoted=draft.quoted, confidence=draft.confidence):
                refined += 1
            else:
                # 用户改过 / 已完成 / 撞了别的待办：以现状为准，不改
                self._say(f"#{task_id} 已经变了（或已完成），这次精修跳过")

        # 模型比规则多找出来的：补成新待办（去重照旧走 save_drafts）
        for extra in drafts[len(pairs):]:
            self.store.save_drafts(message, [extra])

        if refined:
            self._say(f"精修了 {refined} 条")

    def _say(self, text: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(text)
            except Exception:
                pass
