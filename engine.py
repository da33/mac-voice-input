"""錄音與語音辨識。

用 macOS 26 內建的蘋果語音辨識：免費、在本機跑、聲音不上傳、幾乎不佔記憶體。
"""

from __future__ import annotations

import io
import json
import queue
import re
import select
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

APPLE_HELPER = Path(__file__).resolve().parent / "app" / "apple-speech"
APPLE_LOCALE = "zh-TW"
APPLE_TIMEOUT = 20.0
AUDIO_TIMEOUT = 4.0  # 開／關麥克風超過這麼久沒回應，就當作 macOS 音訊系統卡死了


def input_device_names() -> list[str]:
    names = []
    for dev in sd.query_devices():
        if dev["max_input_channels"] > 0 and dev["name"] not in names:
            names.append(dev["name"])
    return names


def refresh_devices() -> None:
    """重新初始化 PortAudio，才抓得到剛插上／剛切換的麥克風（約 20ms）。"""
    sd._terminate()
    sd._initialize()


class RecorderHung(Exception):
    pass


class Recorder:
    """錄音。開、關麥克風都交給背景執行緒做：macOS 的音訊系統偶爾會卡死，
    卡在背景的話，快捷鍵跟浮動條還能動，也能發現卡住了自動重開。"""

    def __init__(self, microphone: str = ""):
        self.microphone = microphone
        self.sample_rate = 16000
        self.level = 0.0
        self._chunks: list[np.ndarray] = []
        self._stream = None
        self._jobs: queue.Queue = queue.Queue()
        self._busy_since = None  # 背景正在處理的那件事是什麼時候開始的
        threading.Thread(target=self._worker, name="audio", daemon=True).start()

    def start(self) -> None:
        self._run(self._open_stream, AUDIO_TIMEOUT)

    def stop(self) -> tuple[np.ndarray, int]:
        stream, self._stream = self._stream, None
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        self._chunks = []
        self.level = 0.0
        if stream is not None:
            self._jobs.put(lambda: self._close(stream))  # 不等它關好，放開按鍵馬上就能辨識
        return audio, self.sample_rate

    def device_names(self, refresh: bool) -> list[str]:
        """選單用的麥克風清單；音訊系統卡住時回傳空清單，不讓選單跟著卡住。"""
        def names():
            if refresh:
                refresh_devices()
            return input_device_names()

        try:
            return self._run(names, 1.0)
        except Exception:
            return []

    def hung(self) -> bool:
        since = self._busy_since
        return since is not None and time.monotonic() - since > AUDIO_TIMEOUT

    def _run(self, job, timeout: float):
        if self.hung():
            raise RecorderHung("音訊系統卡住了")
        done = threading.Event()
        result = {}

        def task():
            try:
                result["value"] = job()
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self._jobs.put(task)
        if not done.wait(timeout):
            raise RecorderHung("音訊系統沒有回應")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            self._busy_since = time.monotonic()
            try:
                job()
            except Exception as exc:
                print(f"關麥克風失敗：{exc!r}")
            self._busy_since = None

    def _open_stream(self) -> None:
        refresh_devices()
        device = self._find_device()
        self._chunks = []
        self.level = 0.0
        try:
            self._stream = self._open(device, 16000)
        except sd.PortAudioError:
            rate = int(sd.query_devices(device, kind="input")["default_samplerate"])
            self._stream = self._open(device, rate)
        self._stream.start()

    def _close(self, stream) -> None:
        stream.stop()
        stream.close()

    def _find_device(self):
        if not self.microphone:
            return None  # 系統預設麥克風
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and self.microphone in dev["name"]:
                return i
        print(f"找不到名稱含「{self.microphone}」的麥克風，改用系統預設")
        return None

    def _open(self, device, rate: int):
        stream = sd.InputStream(
            device=device, samplerate=rate, channels=1, dtype="float32", callback=self._callback
        )
        self.sample_rate = rate
        return stream

    def _callback(self, indata, frames, time_info, status) -> None:
        self._chunks.append(indata[:, 0].copy())
        self.level = float(np.sqrt(np.mean(indata**2)))


# ---------------------------------------------------------------- 蘋果內建辨識


class AppleSpeechError(Exception):
    pass


class AppleSpeech:
    """讓 app/apple-speech 小幫手一直在背景待命，每句話丟一個 wav 檔給它。"""

    def __init__(self, locale: str = APPLE_LOCALE):
        self.locale = locale
        self.process = None
        self.temp_dir = Path(tempfile.mkdtemp(prefix="voice-type-"))

    def start(self) -> None:
        self.stop()
        if not APPLE_HELPER.exists():
            raise AppleSpeechError("找不到蘋果辨識小幫手，請執行「安裝App.command」")
        self.process = subprocess.Popen(
            [str(APPLE_HELPER), self.locale],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        reply = self._read(timeout=180)  # 語言包還沒下載的話，第一次要等它下載
        if not reply.get("ready"):
            self.stop()
            raise AppleSpeechError(reply.get("error", "沒辦法啟動"))
        self.transcribe(np.zeros(8000, dtype=np.float32), 16000)  # 預熱，第一句才不會慢

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if self.process is None or self.process.poll() is not None:
            self.start()
        path = self.temp_dir / "recording.wav"
        path.write_bytes(wav_bytes(audio, sample_rate))
        try:
            self.process.stdin.write(json.dumps({"path": str(path)}) + "\n")
            self.process.stdin.flush()
            reply = self._read(timeout=APPLE_TIMEOUT)
        except OSError as exc:  # 小幫手意外結束
            self.stop()
            raise AppleSpeechError("小幫手沒有回應") from exc
        finally:
            path.unlink(missing_ok=True)
        if "error" in reply:
            raise AppleSpeechError(reply["error"])
        return reply.get("text", "")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        self.process = None

    def _read(self, timeout: float) -> dict:
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        line = self.process.stdout.readline() if ready else ""
        if not line:
            self.stop()
            raise AppleSpeechError("小幫手沒有回應")
        return json.loads(line)


# ---------------------------------------------------------------- 辨識執行緒


class Transcriber(threading.Thread):
    """背景執行緒：依序把錄音轉成文字。

    cfg 是跟主程式共用的設定 dict，每次辨識都重新讀，所以從選單改自動更正會立刻生效；
    蘋果辨識沒辦法啟動時呼叫 reload() 重試。
    """

    def __init__(self, cfg: dict, on_state, on_done):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.on_state = on_state  # on_state(ready, error, note)
        self.on_done = on_done  # on_done(text, error, seconds, took, source, note)
        self.jobs: queue.Queue = queue.Queue()
        self.apple = AppleSpeech()
        self.reload()

    def reload(self) -> None:
        self.jobs.put(None)

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                self._start_engine()
                continue

            audio, sample_rate, seconds = job
            started = time.perf_counter()
            try:
                text, source, note = self._transcribe(audio, sample_rate)
                error = None
            except Exception as exc:
                text, source, note, error = "", "", None, exc
            self.on_done(text, error, seconds, time.perf_counter() - started, source, note)

    def _start_engine(self) -> None:
        self.on_state(False, None, None)
        try:
            self.apple.start()
        except AppleSpeechError as exc:
            self.on_state(False, exc, None)
            return
        self.on_state(True, None, None)

    def _transcribe(self, audio: np.ndarray, sample_rate: int):
        try:
            text = self.apple.transcribe(audio, sample_rate)
        except AppleSpeechError as exc:
            # 小幫手偶爾會意外結束，重新叫起來再試一次，這句才不會漏掉
            print(f"⚠️  蘋果辨識失敗：{exc}，重試一次")
            self.apple.stop()
            text = self.apple.transcribe(audio, sample_rate)
        return apply_replacements(tidy_apple_text(text), self.cfg["replacements"]), "蘋果", None


# ---------------------------------------------------------------- 文字整理


def tidy_apple_text(text: str) -> str:
    """蘋果辨識會在標點前、數字旁多塞空格（「開會 ，」「下午 3點」），把它拿掉。"""
    text = re.sub(r"\s+([，。！？、；：」』）】])", r"\1", text)
    text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿0-9])", "", text)
    text = re.sub(r"(?<=[0-9])\s+(?=[一-鿿])", "", text)
    return text.strip()


def apply_replacements(text: str, rules: dict) -> str:
    """自動更正：把常聽錯的詞換成正確的。英文不分大小寫、空格可有可無。"""
    for wrong in sorted(rules, key=len, reverse=True):
        right = rules[wrong]
        if not wrong.strip():
            continue
        if wrong.isascii():
            pattern = re.escape(wrong.strip()).replace(r"\ ", r"\s*")
            text = re.sub(
                rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", lambda _m: right, text, flags=re.IGNORECASE
            )
        else:
            text = text.replace(wrong, right)
    return text


def format_replacements(rules: dict) -> str:
    return "\n".join(f"{wrong} → {right}" for wrong, right in rules.items())


def parse_replacements(text: str) -> dict:
    rules = {}
    for line in text.splitlines():
        parts = re.split(r"\s*(?:→|->|=>|＝|=)\s*", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            rules[parts[0]] = parts[1]
    return rules


def wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()
