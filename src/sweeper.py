"""到期床位承诺的后台清扫。

服务重启后先同步回收一次（占用本身持久化在 SQLite 中，重启不丢失，
只是到期状态需要在恢复时结算），之后由守护线程周期性回收。
"""
import threading
from typing import Any, Callable, Optional


class CommitmentSweeper:
    def __init__(self, expire_fn: Callable[[], Any], interval_seconds: float = 30.0) -> None:
        self.expire_fn = expire_fn
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sweep_once(self) -> int:
        try:
            return len(self.expire_fn())
        except Exception:
            # 清扫失败不影响主流程，下个周期会重试
            return 0

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sweep_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        # 启动时先结算一次，覆盖服务停机期间到期的占用
        self.sweep_once()
        self._thread = threading.Thread(target=self._run, name="bed-commitment-sweeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
