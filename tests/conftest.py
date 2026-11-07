"""Hub 测试的单例超时：用例挂住（如 mock 让循环永不退出）时按 SIGALRM 判失败，不拖死整轮。"""
import signal
import threading

import pytest

# 秒；当前单例都在 1 秒内，留足余量给慢机器
TEST_TIMEOUT = 30


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    # 真实环境验收（SANDBOXHUB_LIVE=1 才跑）要起容器、等挂载与后台任务，不受单例上限约束
    live = item.path.name == "test_work_area_live.py"
    if live or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expire(signum, frame):
        pytest.fail(f"用例超过 {TEST_TIMEOUT} 秒未结束", pytrace=False)

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, TEST_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
