"""音频：从任意容器解出 16 kHz 单声道 PCM，并按响度做归一化。

教室远场录音的电平通常偏低，直接喂给 Whisper 会掉内容，
先统一到目标 RMS 再峰值限幅，实测能明显减少漏句。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
TARGET_RMS_DBFS = -20.0
PEAK_CEILING = 0.95


def decode(path: Path) -> np.ndarray:
    """返回 [-1, 1] 区间的 float32 单声道波形。"""
    import av

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError(f"{path} 没有音轨")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        parts: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                parts.append(out_frame.to_ndarray().reshape(-1))
        for out_frame in resampler.resample(None):
            parts.append(out_frame.to_ndarray().reshape(-1))
    if not parts:
        raise ValueError(f"{path} 解码不到音频帧")
    return np.concatenate(parts).astype(np.float32) / 32768.0


def normalize(samples: np.ndarray) -> tuple[np.ndarray, float]:
    """把整段音频拉到目标 RMS，返回 (波形, 施加的增益 dB)。"""
    rms = float(np.sqrt(np.mean(samples**2)) + 1e-12)
    gain_db = TARGET_RMS_DBFS - 20.0 * float(np.log10(rms))
    scaled = samples * (10 ** (gain_db / 20.0))
    peak = float(np.abs(scaled).max())
    if peak > PEAK_CEILING:
        scaled *= PEAK_CEILING / peak
        gain_db -= 20.0 * float(np.log10(PEAK_CEILING / peak))
    return scaled, gain_db


def write_wav(samples: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if (handle.getnchannels(), handle.getframerate(), handle.getsampwidth()) != (1, SAMPLE_RATE, 2):
            raise ValueError(f"{path} 不是 16 kHz 单声道 16-bit WAV")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def prepare(path: Path, target_wav: Path) -> tuple[Path, float]:
    """解码 + 归一化 + 落盘，返回 (wav 路径, 总时长秒)。"""
    samples = decode(path)
    samples, _gain_db = normalize(samples)
    write_wav(samples, target_wav)
    return target_wav, samples.size / SAMPLE_RATE
