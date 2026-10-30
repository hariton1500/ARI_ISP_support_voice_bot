"""Замер скорости TTS-движков на типовых фразах голосового бота.

Скрипт нужен, чтобы выбирать синтез не по маркетинговым обещаниям, а по
реальным цифрам на конкретной машине. Для каждого движка измеряются:

* ``load_ms``   — время загрузки модели (платится один раз при старте);
* ``ttfb_ms``   — время до первого куска аудио (то, что слышит абонент);
* ``total_ms``  — полное время синтеза фразы;
* ``audio_ms``  — длительность полученного звука;
* ``rtf``       — real-time factor = total_ms / audio_ms (меньше 1 — быстрее
  реального времени).

Дополнительно каждый результат сохраняется в WAV 8 кГц (формат AudioSocket),
чтобы фразы можно было прослушать и сравнить на слух. Для облачных движков
без WAV сохраняется исходный контейнер (MP3).

Движки подключаются лениво: если пакет не установлен, движок пропускается,
остальные продолжают работать.

Примеры:

    python tools/tts_benchmark.py --engines piper --piper-model ^
        models/ru_RU-denis-medium.onnx --out tts-bench
    python tools/tts_benchmark.py --engines edge --edge-voice ru-RU-SvetlanaNeural
    python tools/tts_benchmark.py --engines silero --repeat 3

Результат: ``tts-bench/results.json``, ``tts-bench/results.csv`` и аудио рядом.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio.pcm import (  # noqa: E402 - путь к пакету добавляется выше
    PCM16_DTYPE,
    SAMPLE_RATE,
    resample,
)

#: Фразы, характерные для сценария поддержки: короткая динамическая вставка,
#: средняя фраза с числами и длинная подсказка меню.
DEFAULT_PHRASES: tuple[tuple[str, str], ...] = (
    ("balance_short", "Ваш баланс 227 рублей 23 копейки."),
    (
        "balance_long",
        "На вашем счёте 227 рублей 23 копейки. Списание абонентской платы "
        "произойдёт двенадцатого ноября.",
    ),
    (
        "menu_long",
        "Здравствуйте! Вы позвонили в службу поддержки. Нажмите один, "
        "чтобы узнать баланс, два — чтобы связаться с оператором.",
    ),
)


@dataclass
class Measurement:
    """Результат одного замера."""

    engine: str
    voice: str
    phrase: str
    chars: int
    load_ms: float
    ttfb_ms: float
    total_ms: float
    audio_ms: float | None
    rtf: float | None
    sample_rate: int | None
    audio_file: str | None = None
    error: str | None = None


@dataclass
class EngineResult:
    """Сырой ответ движка."""

    pcm: bytes = b""
    sample_rate: int = SAMPLE_RATE
    first_chunk_at: float | None = None
    container: str = "wav"
    extra: dict[str, object] = field(default_factory=dict)


class Engine:
    """Базовый интерфейс движка для бенчмарка."""

    name = "base"
    voice_name = "-"

    def load(self) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def synthesize(self, text: str) -> EngineResult:  # pragma: no cover
        raise NotImplementedError


class PiperEngine(Engine):
    """Piper (onnxruntime): быстрый локальный синтез, 22.05 кГц, CPU."""

    name = "piper"

    @property
    def voice_name(self) -> str:
        return self._model.stem

    def __init__(
        self,
        model: Path,
        speaker: int | None = None,
        espeak_data_dir: Path | None = None,
    ) -> None:
        self._model = model
        self._speaker = speaker
        self._espeak_data_dir = espeak_data_dir
        self._voice = None

    def load(self) -> None:
        from piper import PiperVoice  # тяжёлый импорт только при запуске

        kwargs = {}
        if self._espeak_data_dir is not None:
            # espeak-ng не открывает данные по путям с не-ASCII символами
            # (например, «— копия»), поэтому каталог можно переопределить.
            kwargs["espeak_data_dir"] = str(self._espeak_data_dir)
        self._voice = PiperVoice.load(str(self._model), **kwargs)

    def synthesize(self, text: str) -> EngineResult:
        if self._voice is None:
            self.load()
        parts: list[bytes] = []
        first: float | None = None
        kwargs = {"speaker_id": self._speaker} if self._speaker is not None else {}
        for chunk in self._voice.synthesize(text, **kwargs):
            if first is None:
                first = time.perf_counter()
            parts.append(chunk.audio_int16_bytes)
        return EngineResult(
            pcm=b"".join(parts),
            sample_rate=int(self._voice.config.sample_rate),
            first_chunk_at=first,
        )


class SileroEngine(Engine):
    """Silero TTS v5_ru (torch, CPU). Лицензия CC BY-NC — только для оценки."""

    name = "silero"

    @property
    def voice_name(self) -> str:
        return self._speaker

    def __init__(self, speaker: str = "baya", sample_rate: int = 8000) -> None:
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._model = None

    def load(self) -> None:
        import torch  # тяжёлый импорт только при запуске

        # v5 отдаёт готовый объект модели (JIT-пакет) и пример текста.
        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker="v5_ru",
            trust_repo=True,
        )
        self._model = model

    def synthesize(self, text: str) -> EngineResult:
        if self._model is None:
            self.load()
        audio = self._model.apply_tts(
            text=text, speaker=self._speaker, sample_rate=self._sample_rate
        )
        first = time.perf_counter()
        samples = np.asarray(audio.detach().cpu().numpy(), dtype=np.float32)
        pcm = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype(PCM16_DTYPE).tobytes()
        return EngineResult(pcm=pcm, sample_rate=self._sample_rate, first_chunk_at=first)


class EdgeEngine(Engine):
    """edge-tts (неофициальный доступ к Microsoft Neural TTS, MP3 24 кГц)."""

    name = "edge"

    @property
    def voice_name(self) -> str:
        return self._voice

    def __init__(self, voice: str = "ru-RU-SvetlanaNeural") -> None:
        self._voice = voice

    def load(self) -> None:
        import edge_tts  # noqa: F401 - проверяем наличие пакета

    def synthesize(self, text: str) -> EngineResult:
        import edge_tts

        async def collect() -> tuple[list[bytes], float | None]:
            communicate = edge_tts.Communicate(text, self._voice)
            chunks: list[bytes] = []
            first: float | None = None
            async for chunk in communicate.stream():
                if chunk["type"] != "audio" or not chunk.get("data"):
                    continue
                if first is None:
                    first = time.perf_counter()
                chunks.append(chunk["data"])
            return chunks, first

        chunks, first = asyncio.run(asyncio.wait_for(collect(), timeout=30.0))
        return EngineResult(
            pcm=b"".join(chunks),
            sample_rate=24000,
            first_chunk_at=first,
            container="mp3",
        )


def _write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    """Записать моно PCM16 в WAV без внешних зависимостей."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


def _save_audio(path: Path, result: EngineResult) -> Path:
    """Сохранить аудио: WAV 8 кГц для локальных движков, контейнер — для MP3."""
    if result.container == "mp3":
        path = path.with_suffix(".mp3")
        path.write_bytes(result.pcm)
        return path
    pcm = result.pcm
    if result.sample_rate != SAMPLE_RATE:
        pcm = resample(pcm, result.sample_rate, SAMPLE_RATE)
    path = path.with_suffix(".wav")
    _write_wav(path, pcm)
    return path


def measure(
    engine: Engine,
    phrases: Iterable[tuple[str, str]],
    out_dir: Path,
    *,
    repeat: int = 2,
    warmup: bool = True,
    save_audio: bool = True,
    log: Callable[[str], None] = print,
) -> tuple[list[Measurement], float]:
    """Прогнать фразы через движок и вернуть замеры и время загрузки модели."""
    phrases = tuple(phrases)
    started = time.perf_counter()
    engine.load()
    load_ms = (time.perf_counter() - started) * 1000.0
    log(f"[{engine.name}] модель загружена за {load_ms:.0f} мс")

    if warmup:
        # Первый вызов на CPU включает прогрев ONNX/torch — он не должен
        # попадать в статистику «горячего» пути.
        engine.synthesize(phrases[0][1])

    results: list[Measurement] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, (name, text) in enumerate(phrases):
        samples: list[Measurement] = []
        for attempt in range(max(1, repeat)):
            started = time.perf_counter()
            try:
                result = engine.synthesize(text)
            except Exception as exc:  # noqa: BLE001 - движок может быть недоступен
                samples.append(
                    Measurement(
                        engine=engine.name,
                        voice=engine.voice_name,
                        phrase=name,
                        chars=len(text),
                        load_ms=load_ms,
                        ttfb_ms=0.0,
                        total_ms=0.0,
                        audio_ms=None,
                        rtf=None,
                        sample_rate=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                break
            total_ms = (time.perf_counter() - started) * 1000.0
            first = result.first_chunk_at
            ttfb_ms = (first - started) * 1000.0 if first else total_ms
            audio_ms = (
                len(result.pcm) / (result.sample_rate * 2) * 1000.0
                if result.container == "wav"
                else None
            )
            audio_file = None
            if save_audio and attempt == 0:
                audio_file = str(_save_audio(out_dir / f"{engine.name}_{index}_{name}", result))
            samples.append(
                Measurement(
                    engine=engine.name,
                    voice=engine.voice_name,
                    phrase=name,
                    chars=len(text),
                    load_ms=load_ms,
                    ttfb_ms=ttfb_ms,
                    total_ms=total_ms,
                    audio_ms=audio_ms,
                    rtf=(total_ms / audio_ms) if audio_ms else None,
                    sample_rate=result.sample_rate,
                    audio_file=audio_file,
                )
            )
            log(
                f"[{engine.name}] {name}: ttfb={ttfb_ms:.0f} мс, "
                f"total={total_ms:.0f} мс, rtf="
                + (f"{total_ms / audio_ms:.3f}" if audio_ms else "n/a")
            )
        results.append(_median(samples))
    return results, load_ms



def _median(samples: list[Measurement]) -> Measurement:
    """Свести несколько прогонов одной фразы к медиане (устойчиво к выбросам)."""
    if len(samples) == 1:
        return samples[0]
    ok = [sample for sample in samples if sample.error is None]
    if not ok:
        return samples[0]
    base = ok[0]
    audio = [sample.audio_ms for sample in ok if sample.audio_ms]
    rtf = [sample.rtf for sample in ok if sample.rtf is not None]
    return Measurement(
        engine=base.engine,
        voice=base.voice,
        phrase=base.phrase,
        chars=base.chars,
        load_ms=base.load_ms,
        ttfb_ms=round(statistics.median(s.ttfb_ms for s in ok), 2),
        total_ms=round(statistics.median(s.total_ms for s in ok), 2),
        audio_ms=round(statistics.median(audio), 2) if audio else None,
        rtf=round(statistics.median(rtf), 4) if rtf else None,
        sample_rate=base.sample_rate,
        audio_file=base.audio_file,
        error=None,
    )


def build_engine(name: str, args: argparse.Namespace) -> Engine:
    """Создать движок по имени с учётом аргументов командной строки."""
    if name == "piper":
        if not args.piper_model:
            raise SystemExit("для piper нужен --piper-model path/to/voice.onnx")
        model = Path(args.piper_model)
        if not model.is_file():
            raise SystemExit(f"модель Piper не найдена: {model}")
        espeak_data = Path(args.piper_espeak_data) if args.piper_espeak_data else None
        if espeak_data is not None and not espeak_data.is_dir():
            raise SystemExit(f"каталог espeak-ng не найден: {espeak_data}")
        return PiperEngine(model, speaker=args.piper_speaker, espeak_data_dir=espeak_data)
    if name == "silero":
        return SileroEngine(speaker=args.silero_speaker)
    if name == "edge":
        return EdgeEngine(voice=args.edge_voice)
    raise SystemExit(f"неизвестный движок: {name}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engines",
        default="piper",
        help="через запятую: piper, silero, edge",
    )
    parser.add_argument("--piper-model", help="путь к .onnx голоса Piper")
    parser.add_argument("--piper-speaker", type=int, default=None, help="speaker_id")
    parser.add_argument(
        "--piper-espeak-data",
        help="каталог espeak-ng-data (нужен, если путь к piper содержит не-ASCII)",
    )
    parser.add_argument("--silero-speaker", default="baya", help="голос Silero v5_ru")
    parser.add_argument("--edge-voice", default="ru-RU-SvetlanaNeural")
    parser.add_argument("--out", default="tts-bench", help="каталог для результатов")
    parser.add_argument("--repeat", type=int, default=2, help="прогонов на фразу")
    parser.add_argument(
        "--text",
        action="append",
        default=None,
        help="своя фраза (можно несколько раз); по умолчанию — типовые",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out)
    phrases = tuple(
        (f"custom_{index}", text) for index, text in enumerate(args.text or [])
    ) or DEFAULT_PHRASES

    all_results: list[Measurement] = []
    loads: dict[str, float] = {}
    for engine_name in [item.strip() for item in args.engines.split(",") if item.strip()]:
        try:
            engine = build_engine(engine_name, args)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            continue
        try:
            results, load_ms = measure(engine, phrases, out_dir, repeat=args.repeat)
        except ImportError as exc:
            print(f"[{engine_name}] пропущен: {exc}", file=sys.stderr)
            continue
        loads[engine_name] = round(load_ms, 2)
        all_results.extend(results)

    if not all_results:
        print("ни один движок не запустился", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "load_ms": loads,
        "results": [asdict(item) for item in all_results],
    }
    (out_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(all_results[0])))
        writer.writeheader()
        for item in all_results:
            writer.writerow(asdict(item))

    print(f"\nрезультаты: {out_dir / 'results.json'}")
    return 0 if any(item.error is None for item in all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

