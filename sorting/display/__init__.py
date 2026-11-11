"""显示层：把当前件画成 480×320 的屏图，以及整机俯视示意图。

    renderer  单件屏图（序号 / 名称 / 图片 / 总数），真机直接送 SPI 屏
    layout    整机俯视 + 路径对比图，只用于报告

渲染不依赖任何硬件，PC 上就能把布局和字体调完——真机上同一份 render()
返回的 PIL Image 交给屏驱动即可。
"""

from . import layout, renderer
from .renderer import DisplayModel, font, render, save

__all__ = ["layout", "renderer", "DisplayModel", "font", "render", "save"]
