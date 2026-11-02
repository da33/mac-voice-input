"""閃退監看：在旁邊看著語音輸入，它閃退的話自動重新打開 App。

由 voice_type.py 從 App 啟動時開起來（約 6MB，平常只是在等，不吃 CPU）。
主程式正常結束前會從 stdin 送 "quit"；沒送就消失，代表閃退了。
macOS 自己的程式碼出錯（例如選單列圖示）會直接把整個程式關掉，程式裡面接不住，只能從外面重開。
"""

import os
import sys
import time

CRASH_ENV = "VOICE_TYPE_CRASH_RESTART"
CRASH_COOLDOWN = 60  # 重新打開後 1 分鐘內又閃退，就不再重開，免得一直閃退一直開


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def main() -> None:
    bundle_id, launcher_pid, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    if sys.stdin.buffer.read() == b"quit":  # 一直等到主程式結束
        return
    try:
        last = float(os.environ.get(CRASH_ENV) or 0)
    except ValueError:
        last = 0.0
    if time.time() - last < CRASH_COOLDOWN:
        print("❌ 語音輸入重新打開後又閃退，不再自動重開，請自己打開「語音輸入.app」", flush=True)
        return
    print("💥 語音輸入閃退了，自動重新打開", flush=True)
    # 等 App 的啟動器也結束，macOS 才會把它當成已經關掉，不然 open 只會切換過去
    deadline = time.monotonic() + 5
    while alive(launcher_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.5)
    if os.path.exists(log_path):
        # 啟動器每次都會清空紀錄檔，先把閃退前的紀錄留下來
        os.replace(log_path, log_path.replace(".log", ".crash.log"))
    os.execv("/usr/bin/open", ["open", "-b", bundle_id, "--env", f"{CRASH_ENV}={time.time()}"])


if __name__ == "__main__":
    main()
