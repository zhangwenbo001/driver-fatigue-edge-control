# 驾驶员疲劳检测边缘控制系统

> 面向作品集的公开脱敏版本。

本项目将 Windows 视觉上位机与 STM32F103ZET6 控制器连接起来，用于驾驶员疲劳告警。上位机通过 YOLOv8 检测眼睛和嘴部状态，将其转换为基于时间的疲劳状态，并向 MCU 发送 DMS1 串口心跳。MCU 基于 FreeRTOS 完成帧校验、链路丢失检测以及 LED/蜂鸣器告警驱动。

```text
USB 摄像头
  -> YOLOv8 检测（closed_eye / closed_mouth / open_eye / open_mouth）
  -> 基于时间的疲劳状态机
  -> DMS1 串口心跳（115200 8N1，2 Hz）
  -> STM32F103ZET6 + FreeRTOS
  -> LED 与有源蜂鸣器告警
```

## 项目亮点

- 将视觉推理、疲劳判定、GUI、串口传输和 MCU 控制拆分为可独立测试的模块。
- 使用基于单调时钟的状态机：连续闭眼 1.5 s 触发 `FATIGUE`，张嘴 1.0 s 触发 `YAWN`；证据缺失或过期时安全回退到 `UNKNOWN`。
- 上位机采用有界的“最新帧”队列，MCU 采用长度为 1 的“最新状态”队列，避免实时系统处理陈旧数据。
- 实现 DMS1 帧格式、序号回绕、2 Hz 心跳、结果过期回退、断线重连退避，以及半包、粘包、超长帧处理和 2 s `LINK_LOST` 检测。
- 使用静态 FreeRTOS 任务、StreamBuffer 和队列对象；栈溢出钩子会关闭蜂鸣器并进入安全停机状态。
- 将 `best.pt` 导出为 ONNX opset 12。在 306 张图像的评估集上，PT 与 ONNX 均达到 mAP50 0.9287、mAP50-95 0.6759；Windows CPU 上的 ONNX Runtime 实测基线为 16.4 FPS、P95 延迟 64.1 ms。

## 仓库结构

```text
host_app/       Windows GUI、YOLO 封装、状态机和串口发布器
configs/        运行时标定配置（`fatigue.yaml`）
firmware/       项目专用的 STM32/FreeRTOS 应用与驱动源码
deployment/     PT 转 ONNX 的导出与评估脚本，以及 ONNX 部署模型
tests/          状态机、协议和集成测试
tools/          串口模拟器
docs/           DMS1 协议说明
```

## 运行 Windows 上位机

```powershell
python -m pip install -r host_app/requirements.txt
Set-Location host_app
python main.py --port COM3 --baud 115200
```

请将 `COM3` 替换为开发板实际使用的串口。启动后，GUI 会创建串口发布器和推理线程；在 GUI 中打开摄像头即可开始运行。

## STM32 固件

`firmware/` 包含项目专用的 FreeRTOS 任务、DMS1 解析器、UART 钩子、LED/蜂鸣器驱动以及 Keil 工程文件。本公开仓库有意不包含第三方 HAL/CMSIS/FreeRTOS 模板源码和构建产物；重新构建 Keil 工程前，请补充匹配的官方 STM32F1 HAL、CMSIS 和 FreeRTOS 依赖。

DMS1 协议说明见 [docs/protocol_dms1.md](docs/protocol_dms1.md)。

## 验证范围

- 发布验证中，36 项疲劳状态单元测试和 56 项集成测试均已通过。
- 已在 306 张图像的测试集上完成 PT/ONNX 等价性评估；源数据集和人脸图像有意未包含在公开仓库中。
- 已发布模型文件仅供作品展示和本地评估使用。
- 因尚无 Linux 目标板，尚未完成 Linux/NPU 目标板实测、厂商特定转换、温度和功耗测试；这些工作不被声明为已完成。

## 隐私与发布说明

本公开版本已排除简历、个人联系方式、学号、学校标识、本机路径、原始数据集与人脸样本图像、本地智能体日志、构建产物和内部测试证据。原始工作项目保持独立且未被改动。
