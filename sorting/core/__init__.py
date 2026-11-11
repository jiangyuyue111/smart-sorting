"""核心逻辑：协议、链路、工艺序列、任务队列、状态机。

    protocol   UART 行协议（指令/应答/故障码）
    link       SerialLink / MockLink / Session
    sequence   单件抓放的动作序列（纯函数，可测）
    jobqueue   检出的工件 -> 抓放顺序
    machine    运行状态机

这一层不知道视觉，也不知道屏幕，只认识"目标点"和"指令"。
"""

from . import jobqueue, link, machine, protocol, sequence

__all__ = ["jobqueue", "link", "machine", "protocol", "sequence"]
