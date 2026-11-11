# sorting/ — 上位机软件

树莓派 4B 上跑的全部软件。**没有相机、没有机器也能完整跑一轮**，
这是有意为之：硬件还没装配时就能把视觉、排程、动作序列、屏幕全部验证到。

## 三行上手

```bash
python -m sorting.app.demo                 # 出一套汇报材料到 out/demo/
python -m sorting.app.main --demo          # 跑一轮完整流程，终端打印每件结果
python -m unittest discover -s sorting/tests -t .    # 91 个测试，无板无相机
```

## 目录

```
sorting/
├─ config/
│   ├─ machine.toml      四个轴的齿轮参数、行程、速度、工艺参数、6 个料盒
│   └─ classes.json      6 类的形状特征 + 色相区间 + 名称（换货只改这里）
├─ core/                 ← 不认识视觉，也不认识屏幕
│   ├─ protocol.py       UART 文本协议：GOTO / Z / GRIP / HOME / STOP / PING
│   ├─ link.py           SerialLink（真串口）、MockLink（模拟 MCU）、Session
│   ├─ sequence.py       单件抓放的动作序列（生成器，纯函数）
│   ├─ jobqueue.py       检出结果 → 抓放顺序（三种排法取最优）
│   └─ machine.py        运行状态机
├─ vision/               ← 只认图像和毫米
│   ├─ geometry.py       单应变换（DLT，4 点求 H）
│   ├─ classes.py        类别簿 + 判定（先定形状，再定颜色，最后组合）
│   ├─ detect.py         分割 → 抽稀 → 特征 → 分类 → 毫米坐标
│   └─ synthetic.py      合成托盘图（没有相机时的替代品，也是测试数据源）
├─ display/
│   ├─ renderer.py       480×320 屏图：序号 / 名称 / 图片 / 总数
│   └─ layout.py         整机俯视 + 路径对比图（报告用）
├─ app/
│   ├─ runner.py         把 Action 变成指令，记录时序
│   ├─ main.py           上机入口：看门狗 + 状态机 + 屏幕线程
│   └─ demo.py           一键出报告材料
└─ tests/                全部可无板运行
```

## 数据流

```
             ┌──────────── 离线一次 ────────────┐
 相机 ──图──▶│ geometry.H⁻¹ ▶ detect ▶ classes  │──▶ Detection(cx_mm, cy_mm, 类别)
             └──────────────────────────────────┘
                          │
                    jobqueue.build
                          ▼
                 Plan（12 件的最短走路顺序）
                          │
                    runner.execute
                          ▼
        Session ── GOTO/Z/GRIP ──▶ MockLink 或 STM32F407
                          │                    │
                          │◀──── OK/DONE/GRIPD ┘
                          ▼
                 Screen（序号/名称/图片/总数）
```

## 三个关键约定

**① 夹爪靠"停在哪"判断有没有夹到。**
TMC2209 的 StallGuard 让夹爪堵转即停。空夹会一直合到 `grip_close_mm`，
夹到工件会停在工件宽度处。所以 `GRIPD ≥ grip_hold_min_mm` 就是夹到了。
这是**位置判据**，不是力度判据，力矩上限在 MCU 侧限制。

**② 所有坐标换算都在配置里。**
齿轮传动 `d = m·z`、`L = πd/i`、`S = 整步数×细分/L`，改装配只改
`machine.toml`，代码一行不动。打印 `--verify` 之类的东西之前先看
`axis.mm_per_rev` 和 `axis.steps_per_mm` 对不对。

**③ 没标定就不许把数当实测。**
`machine.toml` 的 `[meta] calibrated = false` + `pending = [...]` 决定了
报告里所有节拍数字的措辞。装配完成后实测填回、把 `calibrated` 改成 `true`，
报告措辞会自动从"解析估算"变成"实测"。

## 已知边界

| 边界 | 现状 |
|---|---|
| 托盘容量 | 160×160 mm、工件最长 24 mm → 最多约 25 件；赛规 ≥12 件，够用 |
| 密集摆放 | 两件相碰会粘成一个轮廓，检测必失败——这是净空参数的来源 |
| 视觉指标 | 合成图上 12/12 全对、定位误差 ~0.3 mm；这是**自洽性检验**，不是真机精度 |
| 真机节拍 | 解析估算，不含加减速、回程间隙、相机曝光 |

## 尚未实现（等硬件）

| 项 | 现在到哪了 |
|---|---|
| STM32F407 固件 | 纯逻辑（协议解析、速度曲线、状态机）**已写完并测过 124 条断言**；HAL 实现只有骨架，等板子。见 [`firmware/README.md`](firmware/README.md) |
| 相机标定 | **已写完**：`python -m sorting.tools.calibrate`（内参 + 去畸变 + 托盘单应 + 检查图 + 自检）。求解器自检 7.6e-11 mm，等真照片跑一遍 |
| SPI 屏驱动 | 未写。`renderer.render()` 返回 PIL Image，树莓派侧 `img.tobytes()` 喂 spidev 即可 |
