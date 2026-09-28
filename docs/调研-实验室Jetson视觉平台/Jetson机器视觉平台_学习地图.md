# Jetson 机器视觉实训平台 — 学习地图

> 记录日期：2026-09-28
> 设备：局域网 `192.168.100.x` · 用户 `yc` · 学校实验室设备
> 用途：**学习参考**。本文档只整理"有什么、怎么组织的、值得学什么"，不含代码搬运。

---

## 0. 使用边界（先读这一节，30 秒）

设备是实验室的、你有授权使用 → **登录、运行、阅读、学习都没问题**。

但有两句话说清楚，否则后面容易踩坑：

| 常见说法 | 实际情况 |
|---|---|
| "这是开源的" | ❌ **不是**。`rubclassify` 和 `cr3_d435i_classify` 两个仓库里**一个 LICENSE 文件都没有**（已用 `find -iname 'LICENSE*'` 确认）。无 LICENSE = 默认**保留所有权利**。"给了源码" ≠ "开源" |
| "学校买了版权" | ⚠️ 学校买的通常是**使用许可**，不是**著作权**。著作权仍在原作者手里。而且**许可不能自动传递给你**，尤其不能延伸到**校外竞赛作品** |

**能做的**：读代码理解架构、跑起来看效果、照着思路写自己的实现、学 SDK 用法。
**不能做的**：把代码抄进自己的项目 / 参赛作品 / 任何要发布的东西。

> 🔴 **额外红线（工创赛）**：赛规要求"非标零件须自主设计制造，不允许购买成品套件拼装"，且"**同校作品外形雷同 = 全部 0 分**"。这个平台做的是**螺栓螺母分拣装配**（见 §3 `Classes.py`），和你的智能分拣赛项**同域**。参赛前务必确认：**这套平台的作者是否同校/同实验室，是否也有人报了智能分拣。**

---

## 1. 硬件与平台底账

| 项 | 值 |
|---|---|
| 设备 | **NVIDIA Jetson Xavier NX Developer Kit** |
| 架构 | **ARM aarch64**（6 核） |
| 系统 | Ubuntu 20.04.6 LTS（Focal） |
| 内核 | `5.10.216-tegra`（NVIDIA L4T **R35.6.3**，`BOARD: t186ref` / JetPack 5.1.5） |
| 存储 | eMMC **14.7GB**（偏小，项目里有 `rootOnNVMe/` 说明可迁到 NVMe 启动） |
| CUDA | 11.4（`/usr/local/cuda-11.4`） |
| 深度学习 | torch **2.1.0a0+41361538.nv23.06** (cp38, aarch64) · torchvision 0.19.1 · TensorRT 8.5.2.2 · onnxruntime 1.19.2 |
| 图形 | Qt 5.6.3 (`/usr/local/Qt-5.6.3`) · GNOME (`gdm` 运行中) |
| 账号 | `yc` (uid 1000)，**有 sudo 但需密码** |

⚠️ **两个运维提醒**：① 已 EOL 的 Ubuntu 20.04，**294 个安全更新未打**；② `/opt/MVS` 权限是 `drwxrwxrwx`（全局可写）。

---

## 2. 三个项目总览

都在 `/home/yc` 下：

| 项目 | 是什么 | 规模 | 学习价值 |
|---|---|---|---|
| **`rub_yolov11/rubclassify`** | **螺栓螺母无损检测 / 分拣装配实训平台**（YOLOv11） | ~13,500 行（不含 ultralytics） | ⭐⭐⭐⭐⭐ 架构最完整 |
| **`rub_yolov11/cr3_d435i_classify`** | 深度相机 + 六轴机械臂抓取 | ~24,500 行 | ⭐⭐⭐⭐ 手眼标定 + 多线程 |
| **`wood_木板颜色和缺陷`** | 木板稀密度缺陷检测（单机小项目） | 小 | ⭐⭐ 适合入门，有 `CLAUDE.md` |
| `labelImg` | 标注工具（**MIT 开源**，来自 GitHub） | 第三方 | ⭐ 直接去上游拿 |
| `ch343ser_linux-main` | WCH CH343 USB 转串口驱动 | 第三方 | 按需 |

**git 归属（重要）**：

| 仓库 | 提交数 | 作者 | 远程 |
|---|---|---|---|
| `rubclassify` | 60 | 一朵花 / tom_code / tomcode `<183****@qq.com>`、bby `<145****@qq.com>`、zp `<296****@qq.com>` | `http://192.168.1.x/tomcode/rubclassify.git`（**内网私有 Git**） |
| `cr3_d435i_classify` | 44 | 一朵花 / tom_code `<183****@qq.com>`、zp `<296****@qq.com>` | `http://192.168.1.x/tomcode/...` |

实际是 **`tomcode` 和 `zp` 两人**的作品（`一朵花`/`tom_code`/`tomcode` 是同一个人的不同 git 用户名）。`tomcode` 很可能是**厂商名**（"图码"），学校向厂商买了源码授权。

---

## 3. `rubclassify` 架构地图 ★ 最值得学

### 3.1 它做什么

**螺栓/螺母类紧固件的机器视觉检测与分拣装配**。`Classes.py` 顶部注释列出的工件：

```
六角螺母 / 方形螺母 / 花齿六角法兰螺帽 / 压板花齿压铆螺母 / 高头滚花螺母 / 网纹滚花调节螺帽
```

有 `StudentCode.py` / `StudentTask.py` → 这是一套**教学实训平台**，学生填空式完成实验。

### 3.2 分层架构（★ 这是最值得学的部分）

项目用了**接口抽象 + 工厂模式**，把"用哪家相机/哪个机械臂"这件事彻底解耦：

```
interface/                      抽象层（ABC 抽象基类）
  ├─ CamInterface.py            CameraInterface: open/close/capture/is_open/get_last_error
  └─ yoloInterface.py           YOLOvInterface + @dataclass Detection(bbox/score/class_id/class_name/extra)

implement/                      实现层（每种硬件一个实现）
  ├─ DahengCam.py               大恒相机实现
  ├─ haikangImpl.py             海康相机实现
  ├─ yolov11Impl.py             YOLOv11 后端（+ get_yolov11(), ModelType）
  └─ clear_memory.py            显存/内存回收

factory/                        工厂层（按枚举产出实例）
  ├─ cam_factory.py             CameraType{HaiKang, DaHeng} + CamFactory.create_camera()
  └─ robot_factory.py           RobotType{MG400, CR3, M1PRO} + 默认IP + packagePose

CameraWorker.py                 QThread 采集线程（发 Qt 信号到 UI 线程）
DobotWorker.py                  QThread 机械臂控制线程
MAIN_GUI.py + quexianUI.py      PyQt5 主界面（"缺陷UI"）
MAIN_Calibrate.py               手眼标定（1248 行，最重的单文件之一）
Classes.py                      工件类别定义
config.py / my_utils.py         配置 / 工具（含中文绘制 put_chinese_text）
Facial_recognition/             人脸识别模块（另一个独立 demo）
resetCode/                      学生重置代码
```

### 3.3 值得学的 5 个具体点

1. **工厂 + 接口双模式**：`CamFactory.create_camera(CameraType.DaHeng)` — 上层完全不知道底层是哪家相机。**换相机不用改业务代码**。
   - 💡 对你的智能分拣：你的方案里有"USB 摄像头"，如果以后要换型号，这套模式直接抄思路（不是抄代码）。
2. **`@dataclass Detection` 统一检测结果结构**：`bbox / score / class_id / class_name / extra`。后端可以是 PyTorch / ONNX / TensorRT / OpenVINO，上层拿到的结构永远一样。
   - 💡 这是**多后端可替换**的标准做法，`extra` 字段留了 mask、关键点的扩展位。
3. **QThread + 信号槽做采集**：`CameraWorker(QThread)` 发 `signalsImg` / `signalsCam`，UI 线程只管显示。
   - ⚠️ **对照你的踩坑史**：你在 SG2002 上遇到过"非阻塞读退化、相机 DMA 挂死"。这里用独立线程 + 阻塞采集，正是对付这类问题的常规解 —— 值得对比研究。
4. **`MAIN_Calibrate.py`（1248 行）**：完整的手眼标定实现。
   - 💡 **和你的项目直接相关**：你的分拣方案里"内参去畸变 + 仿射/单应"那一步，这里有一份可参考的工程实现（**看思路，别抄**）。
5. **一个反面教材**：`implement/yolov11Impl.py` 和 `implement/yolo11lmpl.py` **大小完全一样（7252 字节）** —— 典型的文件名拼错后复制一份，两份并存没人清理。`requirements.txt` 里也有一堆 `ubuntu-*`、`dbus-python` 之类被 `pip freeze` 一并抓进来的系统包。**说明这是实战项目而非精修项目，读的时候要有分辨力。**

---

## 4. `cr3_d435i_classify` 架构地图

**做什么**：大恒相机 + 六轴机械臂（CR3 / M1Pro / MG400）+ Intel RealSense D435i，**2D 手眼标定 + 抓取**。README 明说采用 **MVC 设计模式**。

```
mvc/                             MVC 模式
  ├─ vendor_dobot_CR3.py         六轴 CR3 的厂商适配
  └─ dobot_4axis_api.py          四轴 API
sdk/cr3/dobot_api.py  (3041行)   越疆 Dobot 官方 SDK
gxipy/                           大恒 Galaxy Python SDK（纯 Python，见 §6）
thread/ArmThread.py  (702行)     机械臂线程
CR3Thread.py         (706行)     机械臂线程（另一版）
Controller/camController.py      相机控制器
Camera/Camera_dahang.py          大恒相机封装
implement/CR3_robot.py           机械臂实现
custom/SQButton.py SQControlPose.py   自定义 Qt 控件
core/                            yolov3 backbone/dataset/utils（较老的检测框架）
mAP/main.py                      评估
GUI.py (1605行) / MainWindow.py / ui/   界面
inc/DobotDll.h  DobotTypes.h     C 头文件
```

**值得学**：`mvc/` 的厂商适配分层、`thread/ArmThread.py` 的机械臂异步控制、`Controller/` 与 `Camera/` 的职责切分。

---

## 5. `wood_木板颜色和缺陷`（入门首选）

**做什么**：木板"稀密度"缺陷分类。相机 = **大恒 MER2-302-56U3C**，模型 = YOLOv11 分类（`wood_cls_best.pt`）。

- `main_window.py` — PyQt5 采集主界面（开/关相机、采集、ROI/增益/曝光/帧率可调、OK/NG 判定）
- `camera_thread.py` — 相机采集线程 + 分类推理线程
- **有 `CLAUDE.md`**（用 Claude Code 建的），里面把架构讲得很清楚，**这是最好的起点**
- ⚠️ 该目录**不是 git 仓库**，没有版本管理

**为什么适合入门**：文件少、只有一个相机一个模型、README 完整、有 `CLAUDE.md` 讲解。先把 `main_window.py` + `camera_thread.py` 读通，再去看 `rubclassify` 的分层架构，就不会被 13,500 行劝退。

---

## 6. 相机专题：大恒 MER2-302-56U3C

### 6.1 规格（★ = 2026-09-28 实机实测值）

USB3 Vision · 2048×1536 · **BayerRG8（彩色，需去马赛克）** · 全局快门

| 项 | 实测 |
|---|---|
| 型号 / 序列号 | MER2-302-56U3C / **FDS25090519** |
| 最大分辨率 | Width **2048** × Height **1536** |
| 像素格式 | **BayerRG8**（`PixelFormat=(17301513,'BayerRG8')`）→ 取图必须 `.convert("RGB")` 去马赛克 |
| 标称 / 实测帧率 | 56.0 fps（`AcquisitionFrameRate`）/ **48.2 fps**（连抓 30 帧实测） |
| 曝光 / 增益 | 10000 µs / 0.0 dB（出厂默认，`Auto` 均为 `Off`） |
| 机身温度 | 39.2 °C |
| 固件版本 | 13.10:12.32:12.10 |
| USB 链路 | **5000 Mbps SuperSpeed**（U3V 的硬要求，掉到 480 就废了） |

**⚠️ 一个费了我一次调试的坑**：gxipy 读参数的接口是**属性式**的 `cam.ExposureTime.get()`，
**不是** `cam.get_int_feature_value("ExposureTime")`（后者静默返回 `None`，不报错，极易误判成"读不到参数"）。
同理枚举信息在 `dm.update_device_list()` 返回的 dict 里，**`U3VDevice` 对象没有 `get_device_info()` 方法**（调用会 `AttributeError`）。

```python
# ✅ 正确的最小取图流程
import gxipy as gx, cv2
dm = gx.DeviceManager()
n, dl = dm.update_device_list()          # 设备信息在这个 dl 里，不在 cam 上
cam = dm.open_device_by_index(1)
cam.ExposureAuto.set(0); cam.ExposureTime.set(20000.0)   # 先关自动再设值
cam.stream_on()
raw = cam.data_stream[0].get_image()
arr = raw.convert("RGB").get_numpy_array()   # BayerRG8 → RGB 必须走 convert
cv2.imwrite("f.jpg", cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
cam.stream_off(); cam.close_device()
```

### 6.2 ★ 最重要的一条：它不是 UVC

**实测确认**：盒子上 `/dev/video*` **一个节点都没有**。

> USB3 Vision / GigE Vision 工业相机**不走 UVC 协议**，所以 **`cv2.VideoCapture()` 读不到它**。你必须用厂商 SDK（`gxipy`）或 Aravis 才能取图。
>
> 这和普通 USB 摄像头是**两套完全不同的东西**——别指望插上就能 `cap.read()`。

### 6.3 三层结构（决定能不能移植）

| 层 | 文件 | 性质 | 可移植性 |
|---|---|---|---|
| Python 封装 | `gxipy/*.py`（`gxiapi.py` 2207行、`gxwrapper.py` 1779行、`gxidef.py`、`dxwrapper.py`） | **纯 Python + ctypes，无任何 `.so`** | 代码跨架构 ✅，但**版权归大恒** ❌ |
| 原生库 | `/usr/lib/libgxiapi.so` | `ELF 64-bit LSB, **ARM aarch64**`, not stripped | ❌ 闭源二进制，锁架构 |
| 协议 | USB3 Vision + **GenICam** | 公开标准 | ✅ 可用开源实现 |

### 6.4 ★ "移植到我自己板子上"的正确做法

**"移植"这个词不成立**——闭源二进制没有源码，谈不上移植。正确做法是**去厂商官网下载对应架构的构建**：

| 你的板子 | 架构 | 结论 |
|---|---|---|
| **树莓派 3/4/5** | aarch64 | ✅ **能用**。大恒 Galaxy 官网提供 aarch64 构建，装上即可（同架构） |
| **SG2002**（LicheeRV Nano） | RISC-V | ❌ 厂商无此构建，闭源也没法自己编译。**死路** |
| **ESP32-S3 / P4** | Xtensa | ❌ 更死，MCU 跑不了 Linux SDK |

**绕过方案**：MVS 的第三方许可清单里有 `GenICam_License.txt`，说明这类相机走公开标准。可用开源库 **Aravis**（LGPL-2.1，支持 GigE Vision + USB3 Vision + GenICam）**在任意 Linux 架构（含 RISC-V）编译**。代价是自己写对接代码。

### 6.5 当前状态：✅ **已完全跑通（2026-09-28 实测）**

相机已物理接入，**端到端取图验证通过**，无需编译任何驱动：

```
[2] device count = 1   model = MER2-302-56U3C  sn = FDS25090519
[3] opened, class = U3VDevice
[5] stream_on OK
[6] raw: 2048x1536  fmt=BayerRG8
[8] numpy: shape=(1536,2048,3) min=13 max=255 mean=97.9   ← 真实画面
[9] SAVED: /tmp/daheng_smoke_test.jpg  527601 bytes
[10] stream_off OK  [11] close_device OK
```

**重要澄清**：用户最初怀疑"可能没有编译安装"—— **这个怀疑不成立，而且方向是错的**：
- USB3 Vision 走 **usbfs 用户态**，**根本不存在内核驱动**，没有东西需要编译
- `lsmod | grep uvc` 为空、`/dev/video*` 不存在，**都是正确现象**，不是故障
- 安装状态已核实：`gxipy 2.0.2103.9041` 在 `/usr/local/lib/python3.8/dist-packages/`，
  udev 规则 `99-galaxy-u3v.rules` + `80-drivers-HikSDK-2bdf.rules` 就位，`yc` 在 `plugdev` 组
- `libgxiapi.so` 是 `ELF 64-bit ARM aarch64` 且 **not stripped**

---

## 7. 依赖与许可清单 ⚠️

**判断"能不能拿"的关键表**：

| 组件 | 许可 | 能不能拿 |
|---|---|---|
| `rubclassify` / `cr3_d435i_classify` 自身代码 | **无 LICENSE** = 保留所有权利 | ❌ **不能** |
| **Ultralytics YOLOv11** | **AGPL-3.0** ⚠️ | ❌ 传染性最强，分发或网络提供服务即须整体开源 |
| ├ 来源 | `-e git+https://gitee.com/monkeycc/ultralytics.git@265b92f...`（fork） | fork 也得守 AGPL |
| `gxipy`（大恒 Python SDK） | 大恒版权（**纯 Python 也≠开源**） | ❌ 不能分发 |
| `haikangSDK/MvImport`（海康） | 海康版权 | ❌ 不能分发 |
| `DobotDllPy` / `sdk/cr3`（越疆） | 越疆版权 | ❌ 不能分发 |
| `/opt/MVS`、`Galaxy_*` | 专有 SDK（禁止再分发） | ❌ 不能分发；**官网可免费下载自用** ✅ |
| CUDA / cuDNN / TensorRT / VPI2 | NVIDIA EULA | ❌ 不能分发 |
| **PyQt5** | **GPL-3.0**（或商业授权） | ⚠️ **另一个传染陷阱**——用它发布的东西必须 GPL |
| OpenCV | Apache-2.0 | ✅ 可用 |
| PyTorch / torchvision | BSD-3 | ✅ 可用 |
| `labelImg` | **MIT** | ✅ 可用（去 `github.com/HumanSignal/labelImg` 拿） |
| `rootOnNVMe` | **MIT** | ✅ 可用（去 `github.com/jetsonhacks/rootOnNVMe` 拿） |

**一句话记法**：**用 SDK 开发 ✅ / 分发 SDK ❌；学架构 ✅ / 抄代码 ❌；要开源组件就去上游拿，别从这台机器上抠。**

### 7.1 所以，我能带走什么？（结论）

**✅ 能直接用**

1. **借这台 Jetson 当开发验证平台** —— 唯一真正有价值的复用。
   它和分拣项目主控（树莓派）**同为 aarch64 + Python**，代码可双向搬运。
   ⚠️ 硬约束：**只用纯 OpenCV + NumPy**。一旦引入 CUDA / TensorRT，代码就锁死在 Jetson 上，搬不回 Pi。
2. **大恒相机 + Galaxy SDK 自用**（学原理）—— SDK 官网免费下载自用合法。
   但**不值得买**：¥2000+ 爆预算，且全局快门的溢价在你"固定俯视"场景用不上。
3. **下列公共知识**（不是别人的代码，是行业常识）：
   - USB3 Vision ≠ UVC：工业相机不出 `/dev/videoN`，`cv2.VideoCapture()` 读不到，`lsusb -t` 里 `Driver=` 为空是**正常的**
   - GenICam 是公开标准 → **Aravis**(LGPL-2.1) 可在任意架构编译（含 RISC-V），是闭源 SDK 的合法绕行方案
   - 相机 SDK 三层结构：协议 / 原生 `.so`（锁架构）/ Python 封装
   - 工厂 + 接口模式解耦硬件；`@dataclass` 统一推理结果；QThread + 信号槽做采集线程
4. **第三方开源件**（**去上游拿**）：`labelImg`(MIT)、`rootOnNVMe`(MIT)、`torchvision`(BSD-3)、`Aravis`(LGPL-2.1)

**📖 只能学思路，不能进代码**

- `rubclassify` 的 interface / factory / implement 分层
- `MAIN_Calibrate.py` 的手眼标定实现 → 与分拣项目"内参去畸变 + 仿射"环节直接相关

**❌ 碰都别碰**

| 东西 | 为什么 |
|---|---|
| 两个仓库的代码本身 | 无 LICENSE = 保留所有权利；作者是 tomcode / zp |
| Ultralytics YOLOv11 | AGPL-3.0，分发或联网提供服务即须整体开源 |
| **PyQt5** | **GPL-3.0** —— 第二个传染陷阱，比 YOLO 更容易被忽略 |
| 任何 SDK 二进制 | 大恒 / 海康 / 越疆 / NVIDIA 均禁止再分发 |

**落到分拣项目**：主控方案**不改**（Pi + ESP32-S3）。Jetson 只在算力上赢，而经典 CV 方案用不到那个算力。
正确用法 = **在 Jetson 上把内参标定 + 仿射变换的代码跑通调稳，再搬到 Pi**，同架构同 Python，基本零改动。
⚠️ **不要为了"用上这块算力"而改成 YOLO** —— "决赛现场换货要能现场调阈值"这个选经典 CV 的理由依然成立。

### 7.2 ❌ 负面清单：这些**不能**用

按"为什么不能用"分五类。**A/B/C 是法律问题（硬红线），D 是技术死路，E 是质量问题。**

#### A. 著作权不清 —— 一分钱都不能碰

| 对象 | 判定依据 |
|---|---|
| `rubclassify` 全部代码（~13,500 行） | `find -iname 'LICENSE*'` 返回空 → **无 LICENSE = 保留所有权利**，而著作权属 `tomcode`/`zp` |
| `cr3_d435i_classify` 全部代码（~24,500 行） | 同上，无 LICENSE |
| `wood_木板颜色和缺陷`（含 `main_window.py` 等） | 同上，且**连 git 都没有**，连作者都追溯不到 |
| `Facial_recognition/` 人脸识别模块 | `rubclassify` 子目录，同上 |
| `StudentCode.py` / `StudentTask.py`（学生填空文件） | 实训平台的一部分，同上 |
| `resetCode/` | 同上 |

> ⚠️ **最容易被忽略的一条**：这套东西是**教学实训平台**（学生填空式完成实验），
> 设计意图就是"给本校学生按课件做实验"。**"学校能用" ≠ "你能拿走"**——
> 授权是发给学校的，**不会自动传递给学生个人**，更**不延伸到校外竞赛作品**。

#### B. 传染性开源许可 —— 用了就得把你自己也开源

| 对象 | 许可 | 后果 |
|---|---|---|
| **Ultralytics YOLOv11** | **AGPL-3.0** | 只要**分发**或**通过网络提供服务**，整个作品必须 AGPL 开源。来源还是 `gitee.com/monkeycc/ultralytics` 的 **fork**，fork 同样受 AGPL 约束 |
| **PyQt5** | **GPL-3.0**（或商业授权） | ⚠️ **比 YOLO 更隐蔽的陷阱** —— "我只是用它写个界面"，但用它发布的东西必须 GPL。**分拣项目的上位机界面千万别抄这套 PyQt5 结构** |

> 📌 **注意区分**：**AGPL/GPL 是"可以用、但会传染"**，和 A 类的"压根没授权"是两回事。
> 如果只是自己学习、不外发，AGPL 不触发；**一旦要交竞赛作品或放网上，立刻触发**。

#### C. 专有 SDK —— 能用（开发）不能传（分发）

| 对象 | 位置 | 约束 |
|---|---|---|
| 大恒 Galaxy SDK | `/usr/lib/libgxiapi.so`（aarch64）、`~/rub_yolov11/Galaxy_Linux*` | 禁止再分发；**官网可免费下载自用** |
| 大恒 `gxipy` Python 封装 | `cr3_d435i_classify/gxipy/`（纯 Python，无 `.so`） | ⚠️ **纯 Python 也是大恒版权**，"能读到源码" ≠ "开源" |
| 海康 MVS | `/opt/MVS`（`libMvCameraControl.so`） | 禁止再分发 |
| 海康 `haikangSDK/MvImport` | `rubclassify/` | 同上 |
| 越疆 Dobot SDK | `sdk/cr3/dobot_api.py`、`inc/DobotDll.h`、`mvc/vendor_dobot_CR3.py` | 同上 |
| NVIDIA CUDA / cuDNN / TensorRT / VPI2 | `/usr/local/cuda-11.4` 等 | NVIDIA EULA，禁止再分发 |
| NVIDIA 版 PyTorch | `torch 2.1.0a0+41361538.nv23.06`（NGC 构建） | 同上；且**这个 build 只存在于 aarch64+JetPack**，装到别的平台没有 |

#### D. 技术上根本走不通（不是许可问题，是死路）

| 想法 | 为什么死 |
|---|---|
| "把大恒相机驱动移植到我 SG2002 板子上" | `libgxiapi.so` 是**闭源 aarch64 二进制**，RISC-V 无构建、无源码**无法自行编译** → 死路 |
| "移植到 ESP32-S3 / P4" | Xtensa MCU 跑不了 Linux 用户态 SDK → 更死 |
| "在 SG2002 上跑这套 YOLOv11 管线" | 无 aarch64/PyTorch 生态；SG2002 是 RISC-V，且 torch 那个 NGC build 不存在 |
| "`cv2.VideoCapture()` 读这台大恒相机" | U3V **不是 UVC**，不出 `/dev/videoN`，OpenCV 普通路径**读不到**（实测确认无 `/dev/video*`） |
| "把 `libgxiapi.so` 拷到我电脑上就能用" | 架构锁：x86 PC 上装 aarch64 的 `.so` 直接报 `Exec format error`。**要用就去官网下对应架构版本** |

#### E. 工程上也不该学的（反面教材，读的时候要有分辨力）

| 现象 | 问题 |
|---|---|
| `implement/yolov11Impl.py` 与 `implement/yolo11lmpl.py` | **文件大小完全相同（7252 字节）** —— 文件名 `I`/`l` 拼错后复制一份，两份并存没人清理 |
| `requirements.txt` 里的 `ubuntu-*`、`dbus-python` | 被 `pip freeze` 一并抓进来的系统包，**不是项目依赖**，照抄会污染环境 |
| `wood_木板颜色和缺陷/` 无版本管理 | 没有 git，改坏了回不去 |
| 用 SDK 的 Python 封装当"开源参考" | 见 C 类 —— 能读源码不代表能用 |

#### F. 落到分拣项目：具体**不能**做的

1. ❌ **不要把 Jetson 换成主控**。算力用不上，反而引入"设备是借的"这个交付风险。
2. ❌ **不要在分拣代码里 `import torch` / 用 TensorRT / 用 CUDA** —— 一旦用了，代码就搬不回 Pi。
3. ❌ **不要买这台大恒相机**（¥2000+ 爆 ¥3250 预算，全局快门在你的固定俯视场景无优势）。
4. ❌ **不要抄 PyQt5 那套界面结构**（GPL-3.0 传染）。
5. ❌ **不要抄任何 `MAIN_Calibrate.py` 的代码**，只能读思路自己重写。
6. 🔴 **参赛前必查**：这套平台做的是**螺栓螺母分拣装配**，与你的**智能分拣赛项同域**。
   赛规明写"**同校作品外形雷同 = 全部 0 分**"。**务必确认这套平台的作者是否同校同实验室、是否也有人报了智能分拣。**

---

## 8. 建议的学习路径

**第一阶段 — 跑起来（1~2 天）**
1. ~~物理接上 MER2-302-56U3C~~ → ✅ 已接入，SDK 已装，取图已验证（见 §6.5）
   - 建议先**自己写**一个最小取图脚本跑通，建立"我能独立驱动这台相机"的信心
2. 读 `wood_木板颜色和缺陷/CLAUDE.md` + `README.md`，跑 `python3 main_window.py`
3. 确认相机能被识别、能预览、能采集 —— **先建立"这套东西能跑"的实感**

**第二阶段 — 学最小架构（3~5 天）**
4. 精读 `wood` 项目的 `main_window.py`（Qt 界面）+ `camera_thread.py`（采集线程 + 推理线程）
5. 重点理解：**采集线程怎么把图交给 UI 线程而不卡界面**

**第三阶段 — 学分层设计（1~2 周）**
6. 读 `rubclassify/interface/`（两个 ABC，很短）
7. 读 `rubclassify/factory/`（两个工厂，很短）
8. 再回头看 `implement/DahengCam.py` 和 `haikangImpl.py` —— 体会"同一接口两种实现"
9. 读 `CameraWorker.py`（121 行，短）

**第四阶段 — 专题深挖（按需）**
10. `MAIN_Calibrate.py` — 手眼标定，**和你分拣项目的标定环节直接相关**
11. `cr3_d435i_classify/mvc/` + `thread/ArmThread.py` — 机械臂异步控制

**第五阶段 — 动手（在自己的代码里）**
12. 用 Aravis 或 Galaxy SDK **自己写**一个取图程序，不看它的代码
13. 对照着看差异 —— **这才是真正的学习**

---

## 9. 待确认 / 待办

- [ ] **这套平台的作者是否同校/同实验室？是否也有人报智能分拣？**（雷同风险，参赛前必查）
- [ ] 学校买的授权**具体条款**是什么？（能否给学生用于课程外的学习？能否用于竞赛？）—— 建议问实验室老师要授权文件
- [x] ~~相机需物理接入才能做实验~~ → ✅ **2026-09-28 已完成**，端到端取图跑通（见 §6.5）
- [ ] eMMC 只有 14.7GB，如果要长期用建议迁 NVMe（项目里的 `rootOnNVMe` 就是干这个的）
- [ ] 系统是 EOL 的 Ubuntu 20.04，294 个安全更新未打

---

## 附：接入方式备忘

```bash
# 调研者本地 WSL（已配好；密钥未入库）
ssh box            # → yc@192.168.100.x，用专用密钥 ~/.ssh/yc_ubuntu_box
```

密钥是 2026-09-28 为本次排查专门生成的，**只存在于调研者本机，未进本仓库**。
不再需要时，在盒子的 `~/.ssh/authorized_keys` 里删掉 `claude-code-box-recon` 那一行即彻底失效。

> 需要访问的同学请单独向实验室申请，**不要共用同一把密钥**。
