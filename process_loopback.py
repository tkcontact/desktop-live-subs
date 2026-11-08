"""プロセス単位のループバック録音: このプロセス (と子プロセス) 以外が鳴らしている音をまとめて録る

Windows 10 2004 以降の機能 (ActivateAudioInterfaceAsync と VAD\\Process_Loopback)。
デバイスのループバックと違って出力デバイスに縛られないので、アプリごとに出力先を変えていても 1 本で全部録れる。
読み上げの日本語 (このプロセスが鳴らす) は入らないので、読み上げを録って訳し直すことがない。
"""

import ctypes
import logging
import os
import threading
import time
from ctypes import HRESULT, POINTER, byref, c_uint32, c_uint64, c_void_p, wintypes

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, COMObject, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX

log = logging.getLogger(__name__)

_LOOPBACK = 0x00020000
_EVENTCALLBACK = 0x00040000
_AUTOCONVERTPCM = 0x80000000
_SRC_DEFAULT_QUALITY = 0x08000000
_BUFFERFLAGS_SILENT = 0x2
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_EXCLUDE_TARGET_PROCESS_TREE = 1
_VT_BLOB = 65

_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(c_void_p), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    )


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(ctypes.c_long), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
    )


class IAgileObject(IUnknown):
    # 完了通知は別スレッドから呼ばれるので、どのスレッドから呼んでもよいオブジェクトだと示す
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class _Completion(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, operation):
        self.done.set()


class _ProcessLoopbackParams(ctypes.Structure):
    _fields_ = [("ActivationType", wintypes.DWORD), ("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", wintypes.DWORD)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("reserved1", wintypes.WORD), ("reserved2", wintypes.WORD),
                ("reserved3", wintypes.WORD), ("blob", _Blob)]


def _activate(exclude_pid: int):
    fn = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
    fn.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(_PropVariant),
                   POINTER(IActivateAudioInterfaceCompletionHandler),
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    fn.restype = HRESULT
    params = _ProcessLoopbackParams(_ACTIVATION_TYPE_PROCESS_LOOPBACK, exclude_pid, _EXCLUDE_TARGET_PROCESS_TREE)
    pv = _PropVariant(vt=_VT_BLOB, blob=_Blob(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))
    handler = _Completion()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    fn("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(pv),
       handler.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
    if not handler.done.wait(5):
        raise TimeoutError("プロセス単位のループバックを開けませんでした (応答なし)")
    hr, unk = op.GetActivateResult()
    if hr < 0:
        raise OSError(hr, f"プロセス単位のループバックを開けませんでした (0x{hr & 0xFFFFFFFF:08X})")
    return unk.QueryInterface(IAudioClient)


class ProcessLoopbackAudio:
    """このプロセス以外が鳴らしている音を mono float32 で読む。LoopbackAudio と同じ使い方。"""

    def __init__(self, rate: int, block_sec: float = 0.1):
        self.rate = rate
        self.block_sec = block_sec
        self.device_name = "すべてのアプリの音 (このツールの読み上げを除く)"
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = None
        self._last_data = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="process-loopback")
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise RuntimeError(str(self._error)) from self._error
        self._last_data = time.perf_counter()
        log.info("プロセス単位のループバック録音を開始 (%d Hz, 除外する PID %d)", self.rate, os.getpid())

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def read(self) -> np.ndarray:
        """block_sec 程度の音声を返す。何も鳴っていない間は経過時間ぶんの無音を返す。"""
        time.sleep(self.block_sec)
        if self._error:
            raise RuntimeError(f"ループバック録音が止まりました: {self._error}")
        with self._lock:
            data, self._pending = self._pending, []
        now = time.perf_counter()
        if not data:
            gap = now - self._last_data
            if gap < self.block_sec * 2:
                return np.zeros(0, np.float32)
            self._last_data = now
            return np.zeros(int(self.rate * gap), np.float32)
        self._last_data = now
        return np.concatenate(data)

    def _run(self) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        event = None
        client = None
        try:
            client = _activate(os.getpid())
            fmt = WAVEFORMATEX(wFormatTag=3, nChannels=1, nSamplesPerSec=self.rate, nAvgBytesPerSec=self.rate * 4,
                               nBlockAlign=4, wBitsPerSample=32, cbSize=0)  # 3: IEEE float
            client.Initialize(0, _LOOPBACK | _EVENTCALLBACK | _AUTOCONVERTPCM | _SRC_DEFAULT_QUALITY,
                              2_000_000, 0, byref(fmt), None)  # 共有モード、バッファ 200ms
            event = _kernel32.CreateEventW(None, False, False, None)
            client.SetEventHandle(event)
            capture = client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
            client.Start()
        except BaseException as e:
            self._error = e
            self._ready.set()
            if event:
                _kernel32.CloseHandle(event)
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                _kernel32.WaitForSingleObject(event, 100)
                while capture.GetNextPacketSize():
                    data, frames, flags, _, _ = capture.GetBuffer()
                    if flags & _BUFFERFLAGS_SILENT or not data:
                        x = np.zeros(frames, np.float32)
                    else:
                        x = np.frombuffer(ctypes.string_at(data, frames * 4), np.float32).copy()
                    capture.ReleaseBuffer(frames)
                    with self._lock:
                        self._pending.append(x)
        except BaseException as e:
            log.warning("プロセス単位のループバック録音が止まりました: %s", e)
            self._error = e
        finally:
            try:
                client.Stop()
            except Exception:
                pass
            _kernel32.CloseHandle(event)
            del capture, client
            comtypes.CoUninitialize()
