# DMS1 串口协议 V1

```yaml
protocol: DMS1
version: 1.0
status: formal
basis: Public release baseline
last_updated: 2026-08-01
```

## 1. 物理层

| 参数 | 值 | 说明 |
|---|---|---|
| 波特率 | 115200 | STM32 USART1 配置 |
| 数据位 | 8 | — |
| 校验位 | none | V1 无校验 |
| 停止位 | 1 | — |
| 流控 | none | 板载 CH340C 短距直连 |
| 编码 | ASCII | 所有字段为可打印 ASCII |
| 物理接口 | USB-UART (CH340C) | 精英 V2 `USB_UART` Type-C |
| 主机操作系统 | Windows (COMx) 或 Linux (/dev/ttyUSBx) | 端口名不进入报文 |

## 2. 报文格式

```text
DMS1,<seq>,<state>,<confidence>\r\n
```

### 2.1 字段定义

| 字段 | 偏移 | 类型 | 范围 | 说明 |
|---|---|---|---|---|
| 帧头 | 0 | 字面量 | `DMS1` | 固定四字节协议标识 |
| 分隔符 | 4 | 字面量 | `,` | 字段分隔符 |
| seq | 5 | 十进制无符号整数 | 0–65535 | 循环递增序号，仅用于诊断 |
| 分隔符 | — | `,` | — | — |
| state | — | 枚举字符串 | `NORMAL` `YAWN` `FATIGUE` `UNKNOWN` | 仅限四个主机状态 |
| 分隔符 | — | `,` | — | — |
| confidence | — | 十进制无符号整数 | 0–100 | 当前证据置信度评分 |
| 行尾 | — | `\r\n` | CR (0x0D) + LF (0x0A) | 帧终止符 |

### 2.2 合法报文示例

```text
DMS1,0,UNKNOWN,0\r\n
DMS1,42,NORMAL,95\r\n
DMS1,43,YAWN,88\r\n
DMS1,44,FATIGUE,91\r\n
DMS1,65535,NORMAL,100\r\n
DMS1,0,NORMAL,0\r\n
```

最长合法报文计算（按字段上限）：

```text
DMS1 + , + 65535 + , + FATIGUE + , + 100 + \r\n
= 4 + 1 + 5 + 1 + 7 + 1 + 3 + 2 = 24 字节
```

`max_frame_bytes=63` 是面向恶意/损坏输入的传输安全上限，不是合法报文补齐长度。

### 2.3 非法报文示例

以下报文必须被接收方拒绝（不更新状态，不刷新心跳）：

```text
# 帧头错误
XMS1,0,NORMAL,95\r\n
DMS1,0,NORMAL,95\n          ← 缺少 CR，只有 LF
DMS1,0,NORMAL,95\r           ← 缺少 LF

# state 不在枚举内
DMS1,0,BOOT,0\r\n
DMS1,0,LINK_LOST,0\r\n
DMS1,0,normal,0\r\n          ← 大小写敏感
DMS1,0,,0\r\n                ← 空 state

# confidence 越界
DMS1,0,NORMAL,101\r\n
DMS1,0,NORMAL,-1\r\n

# seq 越界或非数字
DMS1,65536,NORMAL,95\r\n
DMS1,abc,NORMAL,95\r\n

# 字段数量错误
DMS1,0,NORMAL\r\n            ← 缺 confidence
DMS1,0,NORMAL,95,extra\r\n   ← 多余字段

# 超长（超过 63 字节）
DMS1,65535,FATIGUE,100xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\n  ← >63 字节
```

## 3. 心跳与时序

| 参数 | 值 | 说明 |
|---|---|---|
| 主机发送周期 | 500 ms | 2 Hz，即使状态未变化也发送 |
| STM32 超时 | 2000 ms | 最后合法帧后 2 秒进入 LINK_LOST |
| 主机首帧前 | 立即发送 `UNKNOWN,0` | 串口就绪但尚无推理结果 |
| 推理故障 | 立即发送 `UNKNOWN,0` | 摄像头 EOF、模型异常、推理线程退出 |
| 结果陈旧 | 1.5 秒后发送 `UNKNOWN,0` | `result_stale_timeout`，不复用过期结果 |

## 4. seq 序号规则

- 初始值：0（发布器启动后首个发送帧）。
- 每次发送后递增：`seq = (seq + 1) % 65536`。
- 到达 65535 后回绕至 0，合法行为。
- seq 仅用于主机侧诊断（丢帧检测），STM32 不作为拒收依据。

## 5. 状态语义

### 5.1 主机状态（进入报文）

| 状态 | 含义 | confidence |
|---|---|---|
| `NORMAL` | 驾驶员处于正常清醒状态 | `open_eye` 最高置信度 ×100 |
| `YAWN` | 检测到持续张嘴（哈欠） | `open_mouth` 最高置信度 ×100 |
| `FATIGUE` | 检测到持续闭眼（疲劳） | `closed_eye` 最高置信度 ×100 |
| `UNKNOWN` | 通信存活但检测证据不可用 | 固定为 0 |

### 5.2 STM32 本地状态（不进入报文）

| 状态 | 含义 |
|---|---|
| `BOOT` | 上电初始状态 |
| `LINK_LOST` | 2 秒无合法帧，通信丢失 |

### 5.3 优先级

```text
STM32 LINK_LOST > 主机 FATIGUE > 主机 YAWN > 主机 NORMAL
无可靠目标持续超时 → UNKNOWN
```

主机不得发送 `BOOT` 或 `LINK_LOST`。

## 6. 接收要求（STM32 侧参考）

- `max_frame_bytes=63` 包含结尾 CRLF；第 64 字节到达且未成帧时判超长，丢弃至下一个 LF 后重新同步。
- 严格要求 LF 前为 CR。
- USART1 中断只把字节转交 StreamBuffer 并重新挂接接收；完整帧只在非 ISR 的 dms_rx_task 上下文组装和解析。
- 半包等待；粘包逐帧解析；超长帧丢弃至换行重新同步。
- 非法帧不改变状态，也不刷新心跳。
- 2 秒无合法帧进入 LINK_LOST；首个新合法帧到达后恢复。
- 闪灯和蜂鸣器全部使用非阻塞计时。

## 7. V1 不含 CRC 的设计理由

当前链路特征：
- 板载 CH340C，PCB 走线距离 < 5 cm。
- USB 2.0 全速，底层已有 CRC 校验。
- 2 Hz 周期心跳提供应用层活性检测。
- 严格字段校验 + 最大帧长限制。
- 2 秒超时进入安全状态（LINK_LOST）。

以上组合已覆盖原型阶段需求。若未来改用长线、强电磁干扰环境或无线链路，单独立项升级为带 CRC-8 的 V2，不得静默改变 V1。

## 8. 兼容策略

| 版本 | 帧头 | 变更 | 兼容性 |
|---|---|---|---|
| V1 | `DMS1` | 初始版本 | — |
| V2（规划） | `DMS2` | 增加 CRC-8 校验字段 | 帧头不同，可共存识别 |

- 帧头为版本标识。接收方按帧头分发解析器。
- V1 解析器拒绝非 `DMS1` 帧。
- 参数变更（波特率、周期、超时）属于部署配置，不改变协议版本。
