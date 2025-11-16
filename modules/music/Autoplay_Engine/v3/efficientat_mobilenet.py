"""Minimal MobileNetV3 implementation compatible with EfficientAT checkpoints."""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

import torch
import torch.nn.functional as F
from torch import Tensor, nn


def _make_divisible(v: float, divisor: int, min_value: Optional[int] = None) -> int:
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return int(new_v)


def cnn_out_size(length: int, padding: int, dilation: int, kernel: int, stride: int) -> int:
    return int((length + 2 * padding - dilation * (kernel - 1) - 1) / stride + 1)


class ConvNormActivation(nn.Sequential):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int,
        stride: int,
        groups: int = 1,
        norm_layer: Optional[Callable[..., nn.Module]] = None,
        activation_layer: Optional[Callable[..., nn.Module]] = None,
    ) -> None:
        norm_layer = norm_layer or partial(nn.BatchNorm2d, eps=0.001, momentum=0.01)
        activation_layer = activation_layer or nn.ReLU
        padding = (kernel_size - 1) // 2
        layers: List[nn.Module] = [
            nn.Conv2d(
                in_channels,
                out_channels,
                kernel_size=kernel_size,
                stride=stride,
                padding=padding,
                groups=groups,
                bias=False,
            ),
            norm_layer(out_channels),
            activation_layer(inplace=True),
        ]
        super().__init__(*layers)


class SqueezeExcitation(nn.Module):
    def __init__(self, in_channels: int, squeeze_factor: int = 4) -> None:
        super().__init__()
        squeeze_channels = _make_divisible(in_channels // squeeze_factor, 8)
        squeeze_channels = max(1, squeeze_channels)
        self.fc1 = nn.Conv2d(in_channels, squeeze_channels, 1)
        self.fc2 = nn.Conv2d(squeeze_channels, in_channels, 1)
        self.act = nn.Hardswish(inplace=True)
        self.gate = nn.Hardsigmoid(inplace=True)

    def forward(self, x: Tensor) -> Tensor:
        scale = F.adaptive_avg_pool2d(x, 1)
        scale = self.act(self.fc1(scale))
        scale = self.gate(self.fc2(scale))
        return x * scale


class ConcurrentSEBlock(nn.Module):
    """Limited SE module that only handles channel attention (se_dims='c')."""

    def __init__(self, channels: int, se_conf: Optional[dict[str, Any]]) -> None:
        super().__init__()
        dims = (se_conf or {}).get("se_dims")
        if not dims:
            self._se = None
        else:
            if any(dim != 1 for dim in dims):
                raise NotImplementedError("Only channel SE is supported in this embedded build")
            reduction = max(1, int((se_conf or {}).get("se_r", 4)))
            self._se = SqueezeExcitation(channels, squeeze_factor=reduction)

    def forward(self, x: Tensor) -> Tensor:
        if self._se is None:
            return x
        return self._se(x)


@dataclass
class InvertedResidualConfig:
    input_channels: int
    kernel: int
    expanded_channels: int
    out_channels: int
    use_se: bool
    activation: str
    stride: int
    dilation: int
    width_mult: float
    f_dim: int = 0
    t_dim: int = 0

    def __post_init__(self) -> None:
        self.input_channels = self.adjust_channels(self.input_channels, self.width_mult)
        self.expanded_channels = self.adjust_channels(self.expanded_channels, self.width_mult)
        self.out_channels = self.adjust_channels(self.out_channels, self.width_mult)

    @staticmethod
    def adjust_channels(channels: int, width_mult: float, min_value: Optional[int] = None) -> int:
        return _make_divisible(channels * width_mult, 8 if min_value is None else min_value)

    def out_size(self, length: int) -> int:
        return cnn_out_size(length, padding=(self.kernel - 1) // 2, dilation=self.dilation, kernel=self.kernel, stride=self.stride)


class InvertedResidual(nn.Module):
    def __init__(
        self,
        cnf: InvertedResidualConfig,
        se_conf: Optional[dict[str, Any]],
        norm_layer: Callable[..., nn.Module],
        depthwise_norm_layer: Callable[..., nn.Module],
    ) -> None:
        super().__init__()
        if not 1 <= cnf.stride <= 2:
            raise ValueError("Illegal stride value")

        activation_layer: Callable[..., nn.Module] = nn.Hardswish if cnf.activation == "HS" else nn.ReLU

        layers: List[nn.Module] = []
        if cnf.expanded_channels != cnf.input_channels:
            layers.append(
                ConvNormActivation(
                    cnf.input_channels,
                    cnf.expanded_channels,
                    kernel_size=1,
                    stride=1,
                    norm_layer=norm_layer,
                    activation_layer=activation_layer,
                )
            )

        layers.append(
            ConvNormActivation(
                cnf.expanded_channels,
                cnf.expanded_channels,
                kernel_size=cnf.kernel,
                stride=cnf.stride,
                groups=cnf.expanded_channels,
                norm_layer=depthwise_norm_layer,
                activation_layer=activation_layer,
            )
        )

        if cnf.use_se:
            layers.append(ConcurrentSEBlock(cnf.expanded_channels, se_conf))

        layers.append(
            nn.Conv2d(
                cnf.expanded_channels,
                cnf.out_channels,
                kernel_size=1,
                stride=1,
                padding=0,
                bias=False,
            )
        )
        layers.append(norm_layer(cnf.out_channels))

        self.block = nn.Sequential(*layers)
        self.use_res_connect = cnf.stride == 1 and cnf.input_channels == cnf.out_channels

    def forward(self, x: Tensor) -> Tensor:
        result = self.block(x)
        if self.use_res_connect:
            result += x
        return result


class EfficientATMobileNet(nn.Module):
    def __init__(
        self,
        inverted_residual_setting: Sequence[InvertedResidualConfig],
        last_channel: int,
        *,
        num_classes: int = 527,
        dropout: float = 0.2,
        in_conv_kernel: int = 3,
        in_conv_stride: int = 2,
        in_channels: int = 1,
        se_conf: Optional[dict[str, Any]] = None,
        input_dims: Tuple[int, int] = (128, 1000),
    ) -> None:
        super().__init__()
        if not inverted_residual_setting:
            raise ValueError("inverted_residual_setting should not be empty")

        block = InvertedResidual
        norm_layer = partial(nn.BatchNorm2d, eps=0.001, momentum=0.01)
        depthwise_norm_layer = norm_layer

        layers: List[nn.Module] = []
        firstconv_output_channels = inverted_residual_setting[0].input_channels
        layers.append(
            ConvNormActivation(
                in_channels,
                firstconv_output_channels,
                kernel_size=in_conv_kernel,
                stride=in_conv_stride,
                norm_layer=norm_layer,
                activation_layer=nn.Hardswish,
            )
        )

        f_dim, t_dim = input_dims
        f_dim = cnn_out_size(f_dim, 1, 1, in_conv_kernel, in_conv_stride)
        t_dim = cnn_out_size(t_dim, 1, 1, in_conv_kernel, in_conv_stride)
        for cnf in inverted_residual_setting:
            f_dim = cnf.out_size(f_dim)
            t_dim = cnf.out_size(t_dim)
            cnf.f_dim, cnf.t_dim = f_dim, t_dim
            layers.append(block(cnf, se_conf, norm_layer, depthwise_norm_layer))

        lastconv_input_channels = inverted_residual_setting[-1].out_channels
        lastconv_output_channels = 6 * lastconv_input_channels
        layers.append(
            ConvNormActivation(
                lastconv_input_channels,
                lastconv_output_channels,
                kernel_size=1,
                stride=1,
                norm_layer=norm_layer,
                activation_layer=nn.Hardswish,
            )
        )

        self.features = nn.Sequential(*layers)
        self.classifier = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(start_dim=1),
            nn.Linear(lastconv_output_channels, last_channel),
            nn.Hardswish(inplace=True),
            nn.Dropout(p=dropout, inplace=True),
            nn.Linear(last_channel, num_classes),
        )

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(self, x: Tensor) -> Tuple[Tensor, Tensor]:
        x = self.features(x)
        pooled = self.classifier[0](x)
        flat = self.classifier[1](pooled)
        hidden = self.classifier[2](flat)
        hidden = self.classifier[3](hidden)
        hidden = self.classifier[4](hidden)
        logits = self.classifier[5](hidden)
        return logits, hidden


def _mobilenet_v3_conf(
    width_mult: float = 1.0,
    *,
    reduced_tail: bool = False,
    dilated: bool = False,
    strides: Tuple[int, int, int, int] = (2, 2, 2, 2),
) -> Tuple[List[InvertedResidualConfig], int]:
    reduce_divider = 2 if reduced_tail else 1
    dilation = 2 if dilated else 1

    bneck_conf = partial(InvertedResidualConfig, width_mult=width_mult)
    adjust_channels = partial(InvertedResidualConfig.adjust_channels, width_mult=width_mult)

    inverted_residual_setting = [
        bneck_conf(16, 3, 16, 16, False, "RE", strides[0], 1),
        bneck_conf(16, 3, 64, 24, False, "RE", strides[0], 1),
        bneck_conf(24, 3, 72, 24, False, "RE", 1, 1),
        bneck_conf(24, 5, 72, 40, True, "RE", strides[1], 1),
        bneck_conf(40, 5, 120, 40, True, "RE", 1, 1),
        bneck_conf(40, 5, 120, 40, True, "RE", 1, 1),
        bneck_conf(40, 3, 240, 80, False, "HS", strides[2], 1),
        bneck_conf(80, 3, 200, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 184, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 184, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 480, 112, True, "HS", 1, 1),
        bneck_conf(112, 3, 672, 112, True, "HS", 1, 1),
        bneck_conf(112, 5, 672, 160 // reduce_divider, True, "HS", strides[3], dilation),
        bneck_conf(160 // reduce_divider, 5, 960 // reduce_divider, 160 // reduce_divider, True, "HS", 1, dilation),
        bneck_conf(160 // reduce_divider, 5, 960 // reduce_divider, 160 // reduce_divider, True, "HS", 1, dilation),
    ]

    last_channel = adjust_channels(1280 // reduce_divider)
    return inverted_residual_setting, last_channel


def build_mn10_model(*, num_classes: int = 527, width_mult: float = 1.0, input_dims: Tuple[int, int] = (128, 1000)) -> EfficientATMobileNet:
    inverted_residual_setting, last_channel = _mobilenet_v3_conf(width_mult=width_mult)
    se_conf = {"se_dims": [1], "se_r": 4}
    return EfficientATMobileNet(
        inverted_residual_setting,
        last_channel,
        num_classes=num_classes,
        input_dims=input_dims,
        se_conf=se_conf,
    )


def load_mn10_as_model(checkpoint_path: str | Path, *, device: Optional[torch.device] = None) -> EfficientATMobileNet:
    model = build_mn10_model()
    checkpoint = torch.load(Path(checkpoint_path), map_location=device or "cpu")
    model.load_state_dict(checkpoint, strict=False)
    model.eval()
    if device:
        model.to(device)
    return model


__all__ = ["build_mn10_model", "load_mn10_as_model", "EfficientATMobileNet"]
