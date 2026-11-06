"""Text-to-speech with Microsoft Edge voices (free, no key) played through Qt Multimedia."""

import asyncio
import os
import re
import shutil
import tempfile
import threading

import edge_tts
from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

HANGUL_PATTERN = re.compile(r"[가-힣]")


class EdgeSpeechPlayer(QObject):
    playback_started = pyqtSignal()
    # Emitted when audio ends, is stopped, or could not be produced.
    playback_finished = pyqtSignal()
    speech_failed = pyqtSignal(str)

    # Internal: (generation, mp3 path) handed from the synthesis thread to the GUI thread.
    _audio_file_ready = pyqtSignal(int, str)
    _synthesis_error = pyqtSignal(int, str)

    def __init__(self, korean_voice: str, english_voice: str):
        super().__init__()
        self._korean_voice = korean_voice
        self._english_voice = english_voice
        self._temporary_directory = tempfile.mkdtemp(prefix="buddy-speech-")

        self._audio_output = QAudioOutput()
        self._media_player = QMediaPlayer()
        self._media_player.setAudioOutput(self._audio_output)
        self._media_player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._media_player.errorOccurred.connect(self._on_media_error)

        # Every speak()/stop() bumps this so late results from older requests are dropped.
        self._speech_generation = 0
        self._is_busy = False

        self._audio_file_ready.connect(self._on_audio_file_ready)
        self._synthesis_error.connect(self._on_synthesis_error)

    @property
    def is_busy(self) -> bool:
        """True while synthesizing or playing."""
        return self._is_busy

    def speak(self, text: str) -> None:
        self.stop()
        if not text.strip():
            return
        self._speech_generation += 1
        self._is_busy = True
        voice = self._korean_voice if HANGUL_PATTERN.search(text) else self._english_voice
        synthesis_thread = threading.Thread(
            target=self._synthesize_to_file,
            args=(self._speech_generation, text, voice),
            daemon=True,
        )
        synthesis_thread.start()

    def stop(self) -> None:
        self._speech_generation += 1
        was_busy = self._is_busy
        self._is_busy = False
        self._media_player.stop()
        if was_busy:
            self.playback_finished.emit()

    def shutdown(self) -> None:
        self.stop()
        shutil.rmtree(self._temporary_directory, ignore_errors=True)

    def _synthesize_to_file(self, generation: int, text: str, voice: str) -> None:
        output_path = os.path.join(self._temporary_directory, f"speech-{generation}.mp3")
        try:
            asyncio.run(edge_tts.Communicate(text, voice).save(output_path))
            self._audio_file_ready.emit(generation, output_path)
        except Exception as error:  # noqa: BLE001 - network or service failures
            self._synthesis_error.emit(generation, str(error))

    def _on_audio_file_ready(self, generation: int, output_path: str) -> None:
        if generation != self._speech_generation:
            return
        self._media_player.setSource(QUrl.fromLocalFile(output_path))
        self._media_player.play()
        self.playback_started.emit()

    def _on_synthesis_error(self, generation: int, error_message: str) -> None:
        if generation != self._speech_generation:
            return
        self._is_busy = False
        self.speech_failed.emit(f"Speech failed: {error_message}")
        self.playback_finished.emit()

    def _on_media_status_changed(self, media_status: QMediaPlayer.MediaStatus) -> None:
        if media_status == QMediaPlayer.MediaStatus.EndOfMedia and self._is_busy:
            self._is_busy = False
            self.playback_finished.emit()

    def _on_media_error(self, media_error: QMediaPlayer.Error, error_text: str) -> None:
        if media_error == QMediaPlayer.Error.NoError or not self._is_busy:
            return
        self._is_busy = False
        self.speech_failed.emit(f"Audio playback failed: {error_text}")
        self.playback_finished.emit()
