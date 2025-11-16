"""Librosa + EfficientAT MobileNet audio analysis service."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .dependency_manager import ensure_model_file

try:  # Optional heavy imports
    import torch
    from torch import Tensor
    from torch import nn
    import torchaudio
except Exception:  # pragma: no cover - optional dependency
    torch = None  # type: ignore
    Tensor = None  # type: ignore
    nn = None  # type: ignore
    torchaudio = None  # type: ignore

try:
    from .efficientat_mobilenet import load_mn10_as_model
except Exception:  # pragma: no cover - optional dependency
    load_mn10_as_model = None

LOG = logging.getLogger(__name__)

_SAMPLE_RATE = 32000
_MEL_BANDS = 128
_DEFAULT_EMBEDDING_MODEL = "mn10_as"

# Lazy import handle for librosa
_LIBROSA = None


@dataclass
class AnalysisResult:
    """Combined Librosa + MobileNet analysis results."""

    tempo: Optional[float] = None
    loudness: Optional[float] = None
    key: Optional[int] = None
    mode: Optional[int] = None
    embedding: Optional[List[float]] = None
    embedding_model: Optional[str] = None
    embedding_dim: Optional[int] = None
    simple_vibe: Optional[List[float]] = None
    analysis_mode: str = "ml"
    success: bool = False
    error: Optional[str] = None


if torch is not None:

    class AugmentMelSTFT(nn.Module):
        """Minimal EfficientAT mel front-end (no training augmentations)."""

        def __init__(
            self,
            n_mels: int = _MEL_BANDS,
            sr: int = _SAMPLE_RATE,
            win_length: int = 800,
            hopsize: int = 320,
            n_fft: int = 1024,
            fmin: float = 0.0,
            fmax: Optional[float] = None,
        ) -> None:
            super().__init__()
            self.n_mels = n_mels
            self.sr = sr
            self.win_length = win_length
            self.hopsize = hopsize
            self.n_fft = n_fft
            self.fmin = fmin
            self.fmax = fmax or sr // 2
            self.register_buffer("window", torch.hann_window(win_length, periodic=False), persistent=False)
            self.register_buffer(
                "preemphasis",
                torch.tensor([[[-0.97, 1.0]]], dtype=torch.float32),
                persistent=False,
            )
            self._mel_basis: dict[str, Tensor] = {}

        def forward(self, waveform: Tensor) -> Tensor:
            if waveform.dim() == 1:
                waveform = waveform.unsqueeze(0)
            if waveform.dim() != 2:
                raise ValueError("Waveform tensor must be 2D: (batch, samples)")

            # Pre-emphasis filter and STFT power spectrogram
            x = torch.nn.functional.conv1d(waveform.unsqueeze(1), self.preemphasis)
            spec = torch.stft(
                x.squeeze(1),
                n_fft=self.n_fft,
                hop_length=self.hopsize,
                win_length=self.win_length,
                window=self.window,
                center=True,
                pad_mode="reflect",
                normalized=False,
                return_complex=True,
            )
            power = spec.abs().pow(2)

            mel_basis = self._get_mel_basis(device=power.device)
            mel_spec = torch.matmul(mel_basis, power)
            mel_spec = torch.log(mel_spec + 1e-5)
            mel_spec = (mel_spec + 4.5) / 5.0
            return mel_spec

        def _get_mel_basis(self, device: torch.device) -> Tensor:
            key = str(device)
            basis = self._mel_basis.get(key)
            if basis is None:
                fb = torchaudio.functional.create_fb_matrix(
                    self.n_fft // 2 + 1,
                    self.fmin,
                    self.fmax,
                    self.n_mels,
                    self.sr,
                    norm=None,
                    mel_scale="htk",
                )
                basis = fb.to(device)
                self._mel_basis[key] = basis
            return basis

else:  # pragma: no cover - executed only when PyTorch missing

    class AugmentMelSTFT:  # type: ignore
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PyTorch is required for EfficientAT preprocessing")


class EnrichingService:
    """Librosa + EfficientAT analysis entrypoint used by Autoplay V3."""

    def __init__(
        self,
        *,
        analysis_mode: str = "ml",
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        verbose: bool = False,
    ) -> None:
        self._analysis_mode = analysis_mode if analysis_mode in ("ml", "non-ml") else "ml"
        self._requested_model = embedding_model
        self._embedding_model = self._normalize_embedding_model(embedding_model)
        self._verbose = verbose
        self._librosa = None
        self._device = None
        self._model = None
        self._mel = None
        self._available = self._check_availability()
        if self._available:
            mode_desc = "Non-ML mode (Librosa-only)" if self._analysis_mode == "non-ml" else f"ML mode ({self._embedding_model})"
            LOG.info("✅ EnrichingService ready: %s", mode_desc)
        else:
            LOG.warning("⚠️ EnrichingService unavailable (missing dependencies)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        return self._available

    def analyze_track(self, audio_path: str | Path) -> AnalysisResult:
        if not self._available:
            return AnalysisResult(success=False, error="EnrichingService not available")

        audio_path = Path(audio_path)
        if not audio_path.exists():
            return AnalysisResult(success=False, error=f"Audio file not found: {audio_path}")

        try:
            librosa = self._import_librosa()
            audio, sr = librosa.load(str(audio_path), sr=_SAMPLE_RATE, mono=True)
            tempo, loudness, key, mode = self._analyze_flow_librosa(audio, sr)
            simple_vibe = self._compute_simple_vibe(audio, sr, tempo, loudness, key, mode)

            if self._analysis_mode == "ml":
                embedding, model_name, embedding_dim = self._extract_mobile_embedding(audio, sr)
                result = AnalysisResult(
                    tempo=tempo,
                    loudness=loudness,
                    key=key,
                    mode=mode,
                    simple_vibe=simple_vibe,
                    embedding=embedding,
                    embedding_model=model_name,
                    embedding_dim=embedding_dim,
                    analysis_mode="ml",
                    success=True,
                )
            else:
                result = AnalysisResult(
                    tempo=tempo,
                    loudness=loudness,
                    key=key,
                    mode=mode,
                    simple_vibe=simple_vibe,
                    analysis_mode="non-ml",
                    success=True,
                )

            if self._verbose:
                LOG.info(
                    "✅ Analysis complete (%s): tempo=%.1f BPM, loudness=%.2f dB, key=%s, mode=%s",
                    result.analysis_mode,
                    tempo or 0.0,
                    loudness or 0.0,
                    key,
                    "major" if mode == 1 else "minor",
                )
            return result
        except Exception as exc:  # pragma: no cover - runtime errors logged
            LOG.error("Analysis failed for %s: %s", audio_path, exc)
            return AnalysisResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Availability & dependency checks
    # ------------------------------------------------------------------
    def _check_availability(self) -> bool:
        try:
            self._import_librosa()
        except ImportError:
            return False

        if self._analysis_mode == "non-ml":
            return True

        return self._setup_ml_pipeline()

    def _setup_ml_pipeline(self) -> bool:
        if torch is None or torchaudio is None or load_mn10_as_model is None:
            LOG.warning("⚠️ PyTorch + torchaudio are required for ML mode. Falling back to non-ML analysis.")
            self._analysis_mode = "non-ml"
            return True

        checkpoint = ensure_model_file(self._embedding_model)
        if not checkpoint:
            LOG.warning(
                "⚠️ MobileNet checkpoint could not be downloaded automatically. Continuing in non-ML mode until the file is present."
            )
            self._analysis_mode = "non-ml"
            return True

        try:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._model = load_mn10_as_model(checkpoint, device=self._device)
            self._model.eval()
            self._mel = AugmentMelSTFT().to(self._device)
            self._mel.eval()
            return True
        except Exception as exc:
            LOG.error("❌ Failed to initialize EfficientAT MobileNet: %s", exc)
            self._analysis_mode = "non-ml"
            return True

    # ------------------------------------------------------------------
    # Core analysis helpers
    # ------------------------------------------------------------------
    def _extract_mobile_embedding(self, audio: np.ndarray, sr: int) -> Tuple[Optional[List[float]], Optional[str], Optional[int]]:
        if not self._model or not self._mel or torch is None:
            return None, None, None

        if sr != _SAMPLE_RATE:
            librosa = self._import_librosa()
            audio = librosa.resample(audio, orig_sr=sr, target_sr=_SAMPLE_RATE)

        waveform = torch.from_numpy(audio).float().unsqueeze(0)
        waveform = waveform.to(self._device)

        with torch.no_grad():
            mel_spec = self._mel(waveform)
            if mel_spec.dim() == 3:
                mel_spec = mel_spec.unsqueeze(1)
            elif mel_spec.dim() == 2:
                mel_spec = mel_spec.unsqueeze(0).unsqueeze(0)
            logits, features = self._model(mel_spec)
            embedding_tensor = features.squeeze().detach().cpu()
            embedding = embedding_tensor.numpy().tolist()
            return embedding, self._embedding_model, len(embedding)

    def _analyze_flow_librosa(
        self,
        audio: np.ndarray,
        sr: int,
    ) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
        librosa = self._import_librosa()
        try:
            tempo, _ = librosa.beat.beat_track(y=audio, sr=sr)
            tempo = float(tempo)
            rms = librosa.feature.rms(y=audio)[0]
            rms_mean = float(np.mean(rms))
            loudness = 20 * np.log10(rms_mean + 1e-6)
            key, mode = self._detect_key_mode_librosa(audio, sr)
            return tempo, loudness, key, mode
        except Exception as exc:
            LOG.warning("Librosa flow analysis failed: %s", exc)
            return None, None, None, None

    def _detect_key_mode_librosa(self, audio: np.ndarray, sr: int) -> Tuple[Optional[int], Optional[int]]:
        librosa = self._import_librosa()
        try:
            chroma = librosa.feature.chroma_cqt(y=audio, sr=sr)
            chroma_mean = np.mean(chroma, axis=1)
            major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
            minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
            major_profile /= np.sum(major_profile)
            minor_profile /= np.sum(minor_profile)
            chroma_norm = chroma_mean / (np.sum(chroma_mean) + 1e-6)
            correlations = []
            for shift in range(12):
                chroma_shifted = np.roll(chroma_norm, shift)
                major_corr = np.corrcoef(chroma_shifted, major_profile)[0, 1]
                correlations.append(("major", shift, major_corr))
                minor_corr = np.corrcoef(chroma_shifted, minor_profile)[0, 1]
                correlations.append(("minor", shift, minor_corr))
            best_match = max(correlations, key=lambda x: x[2])
            mode = 1 if best_match[0] == "major" else 0
            key = best_match[1]
            return int(key), int(mode)
        except Exception as exc:
            LOG.warning("Key/mode detection failed: %s", exc)
            return None, None

    def _compute_simple_vibe(
        self,
        audio: np.ndarray,
        sr: int,
        tempo: Optional[float],
        loudness: Optional[float],
        key: Optional[int],
        mode: Optional[int],
    ) -> List[float]:
        librosa = self._import_librosa()
        try:
            energy = 0.5
            if loudness is not None:
                energy = np.clip((loudness + 60) / 60, 0.0, 1.0)
            rms = librosa.feature.rms(y=audio)[0]
            rms_std = float(np.std(rms))
            energy = np.clip(energy + rms_std * 0.3, 0.0, 1.0)

            valence = 0.65 if mode == 1 else 0.35 if mode == 0 else 0.5
            spectral_centroid = librosa.feature.spectral_centroid(y=audio, sr=sr)[0]
            centroid_mean = float(np.mean(spectral_centroid))
            centroid_normalized = np.clip(centroid_mean / 4000.0, 0.0, 1.0)
            valence = np.clip(valence + (centroid_normalized - 0.5) * 0.2, 0.0, 1.0)

            danceability = 0.5
            if tempo is not None:
                tempo_score = 1.0 - abs(tempo - 120.0) / 120.0
                danceability = np.clip(tempo_score, 0.2, 0.9)
            onset_env = librosa.onset.onset_strength(y=audio, sr=sr)
            beat_strength = float(np.mean(onset_env)) / 10.0
            danceability = np.clip(danceability + beat_strength * 0.2, 0.0, 1.0)

            spectral_rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sr)[0]
            rolloff_mean = float(np.mean(spectral_rolloff))
            rolloff_normalized = np.clip(rolloff_mean / 8000.0, 0.0, 1.0)
            zero_crossing = librosa.feature.zero_crossing_rate(y=audio)[0]
            zcr_mean = float(np.mean(zero_crossing))
            acousticness = 1.0 - (rolloff_normalized * 0.6 + zcr_mean * 0.4)
            acousticness = np.clip(acousticness, 0.0, 1.0)

            brightness = centroid_normalized
            return [float(energy), float(valence), float(danceability), float(acousticness), float(brightness)]
        except Exception as exc:
            LOG.warning("Simple vibe computation failed: %s", exc)
            return [0.5, 0.5, 0.5, 0.5, 0.5]

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _import_librosa(self):
        global _LIBROSA
        if _LIBROSA is None:
            import librosa  # Lazy import

            _LIBROSA = librosa
        return _LIBROSA

    def _normalize_embedding_model(self, embedding_model: str) -> str:
        model = embedding_model.lower().strip()
        if model in {"panns_mobilenetv2", "panns_cnn14"}:
            LOG.info("ℹ️ Mapping legacy model '%s' to EfficientAT mn10_as", model)
            return _DEFAULT_EMBEDDING_MODEL
        return _DEFAULT_EMBEDDING_MODEL if model not in {"mn10_as"} else model


LibrosaMobileNetService = EnrichingService  # Backwards compatibility alias

__all__ = ["EnrichingService", "AnalysisResult"]
