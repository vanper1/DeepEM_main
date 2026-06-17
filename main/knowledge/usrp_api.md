# USRP 采集端服务 API 调用说明

本文档对应当前部署在 `10.112.210.4` 上的 USRP B210 采集服务。

## 1. 服务信息

| 项目 | 值 |
|------|----|
| HTTP 基础地址 | `http://10.112.210.4:8100` |
| WebSocket 基础地址 | `ws://10.112.210.4:8100` |
| API 版本 | `1.0.0` |
| OpenAPI | `http://10.112.210.4:8100/openapi.json` |
| Swagger UI | `http://10.112.210.4:8100/docs` |
| 当前设备 | `usrp-30B1FDE`（Ettus USRP B210） |
| 服务目录 | `/home/ue/DeepEM` |
| 数据目录 | `/home/ue/DeepEM/backend/runtime/usrp_tasks` |
| 日志文件 | `/tmp/usrp_service.log` |

服务当前未配置 HTTP 鉴权和 TLS，只应在可信内网中使用。

所有 JSON 请求应使用：

```http
Content-Type: application/json
```

所有时间字段均为 UTC ISO 8601 格式，例如：

```text
2026-06-15T08:05:49Z
```

## 2. 快速开始

设置服务地址：

```bash
BASE_URL=http://10.112.210.4:8100
DEV_ID=usrp-30B1FDE
TASK_ID=task-$(date +%Y%m%d-%H%M%S)
```

扫描设备：

```bash
curl -sS -X POST "$BASE_URL/api/usrp/scan"
```

只有设备状态为 `IDLE` 时才能启动采集。

启动 3 秒采集：

```bash
curl -sS -X POST "$BASE_URL/api/usrp/start" \
  -H "Content-Type: application/json" \
  -d "{
    \"task_id\": \"$TASK_ID\",
    \"dev_id\": \"$DEV_ID\",
    \"freq\": 2400000000,
    \"sample_rate\": 1000000,
    \"bandwidth\": 1000000,
    \"gain\": 40,
    \"slice_duration\": 1,
    \"duration\": 3,
    \"antenna\": \"RX2\"
  }"
```

轮询任务状态：

```bash
watch -n 0.5 "curl -sS $BASE_URL/api/usrp/devices | jq"
```

当设备从 `BUSY` 恢复为 `IDLE` 且 `task_id` 变为 `null` 时，采集任务已经结束。

## 3. 接口目录

| 方法 | 路径 | 用途 |
|------|------|------|
| `POST` | `/api/usrp/scan` | 扫描 USRP 硬件并刷新设备表 |
| `GET` | `/api/usrp/devices` | 查询内存中的设备状态 |
| `POST` | `/api/usrp/configure` | 校验并保存采集参数，不启动采集 |
| `POST` | `/api/usrp/start` | 启动后台采集任务 |
| `POST` | `/api/usrp/{dev_id}/stop` | 请求停止采集任务 |
| WebSocket | `/api/usrp/{dev_id}/stream` | 接收状态和实时 FFT 数据 |

## 4. 设备状态

| 状态 | 含义 | 是否允许配置/启动 |
|------|------|-------------------|
| `OFFLINE` | 最近一次扫描未发现设备 | 否 |
| `IDLE` | 设备在线且空闲 | 是 |
| `BUSY` | 正在初始化、采集或写入任务数据 | 否 |
| `ERROR` | 设备探测发生错误 | 否 |

调用方应以接口返回状态为准，不要仅根据设备曾经出现过来判断是否在线。

`current_config` 的行为：

- 调用 `configure` 后保存配置。
- 调用 `start` 后保存本次任务配置，其中包含 `task_id`。
- 任务结束后可能继续保留最近一次配置。
- 再次执行硬件扫描后，空闲设备会使用重新探测得到的状态，`current_config` 通常恢复为 `null`。

## 5. 扫描设备

扫描本机 USRP 硬件，读取设备能力并刷新服务中的设备状态表。

### 请求

```http
POST /api/usrp/scan
```

无请求体。

### curl

```bash
curl -sS -X POST \
  http://10.112.210.4:8100/api/usrp/scan
```

### 成功响应

```json
{
  "devices": [
    {
      "dev_id": "usrp-30B1FDE",
      "dev_ip": null,
      "dev_config": {
        "freq_range": {
          "min": 42000000.0,
          "max": 6008000000.0
        },
        "sample_rate_range": {
          "min": 31250.0,
          "max": 16000000.0
        },
        "bandwidth_range": {
          "min": 200000.0,
          "max": 56000000.0
        },
        "gain_range": {
          "min": 0.0,
          "max": 76.0
        },
        "rx_antennas": [
          "TX/RX",
          "RX2"
        ]
      },
      "status": "IDLE",
      "current_config": null,
      "task_id": null,
      "updated_at": "2026-06-15T08:08:59Z"
    }
  ],
  "found_count": 1,
  "scan_error": null
}
```

### 响应字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `devices` | array | 扫描后的完整设备状态列表 |
| `found_count` | integer | 状态不是 `OFFLINE` 的设备数量 |
| `scan_error` | string/null | 扫描异常信息；正常时为 `null` |

注意：

- 扫描会初始化 UHD 并探测设备能力，B210 实测通常需要约 2 秒。
- 扫描内部异常时，接口仍可能返回 HTTP `200`，错误内容放在 `scan_error` 中。因此必须同时检查 HTTP 状态和 `scan_error`。
- 只有 `found_count > 0` 不足以证明设备可采集，还应检查目标设备状态是否为 `IDLE`。

## 6. 查询设备状态

返回服务内存中维护的设备状态，不重新访问硬件。

### 请求

```http
GET /api/usrp/devices
```

### curl

```bash
curl -sS \
  http://10.112.210.4:8100/api/usrp/devices
```

### 响应

```json
[
  {
    "dev_id": "usrp-30B1FDE",
    "dev_ip": null,
    "dev_config": {
      "freq_range": {
        "min": 42000000.0,
        "max": 6008000000.0
      },
      "sample_rate_range": {
        "min": 31250.0,
        "max": 16000000.0
      },
      "bandwidth_range": {
        "min": 200000.0,
        "max": 56000000.0
      },
      "gain_range": {
        "min": 0.0,
        "max": 76.0
      },
      "rx_antennas": [
        "TX/RX",
        "RX2"
      ]
    },
    "status": "IDLE",
    "current_config": null,
    "task_id": null,
    "updated_at": "2026-06-15T08:08:59Z"
  }
]
```

### 设备字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `dev_id` | string | 设备 ID，格式为 `usrp-{serial}` |
| `dev_ip` | string/null | 网络设备 IP；USB 设备为 `null` |
| `dev_config` | object/null | 硬件能力范围 |
| `status` | string | `OFFLINE`、`IDLE`、`BUSY` 或 `ERROR` |
| `current_config` | object/null | 最近保存或正在使用的采集配置 |
| `task_id` | string/null | 当前运行任务 ID |
| `updated_at` | string | 状态更新时间，UTC |

## 7. 配置采集参数

校验并保存采集配置，但不打开 USRP、不创建数据文件，也不启动采集。

`start` 接口仍要求提交全部参数，不会自动复用 `configure` 保存的配置。

### 请求

```http
POST /api/usrp/configure
Content-Type: application/json
```

```json
{
  "dev_id": "usrp-30B1FDE",
  "freq": 2400000000,
  "sample_rate": 1000000,
  "bandwidth": 1000000,
  "gain": 40,
  "slice_duration": 1,
  "duration": 10,
  "antenna": "RX2"
}
```

### 参数

| 字段 | 类型 | 必填 | 约束 |
|------|------|------|------|
| `dev_id` | string | 是 | 必须是扫描得到的设备 ID |
| `freq` | number | 是 | 中心频率，Hz；必须在设备范围内 |
| `sample_rate` | number | 是 | 采样率，Sps；必须在设备范围内 |
| `bandwidth` | number | 是 | 接收带宽，Hz；必须在设备范围内且不能大于 `sample_rate` |
| `gain` | number | 是 | 接收增益，dB；必须在设备范围内 |
| `slice_duration` | number | 是 | 目标切片时长，秒；应大于 0 |
| `duration` | number/null | 否 | 总采集时长，秒；`null` 表示持续采集 |
| `antenna` | string/null | 否 | `TX/RX`、`RX2` 或 `null` |

对于当前 B210，扫描得到的典型能力为：

| 参数 | 范围 |
|------|------|
| `freq` | `42,000,000` 到 `6,008,000,000` Hz |
| `sample_rate` | `31,250` 到 `16,000,000` Sps |
| `bandwidth` | `200,000` 到 `56,000,000` Hz |
| `gain` | `0` 到 `76` dB |
| `antenna` | `TX/RX`、`RX2` |

应始终使用最近一次 `scan` 返回的实际范围，不要把上表硬编码为所有设备的固定值。

### curl

```bash
curl -sS -X POST \
  http://10.112.210.4:8100/api/usrp/configure \
  -H "Content-Type: application/json" \
  -d '{
    "dev_id": "usrp-30B1FDE",
    "freq": 2400000000,
    "sample_rate": 1000000,
    "bandwidth": 1000000,
    "gain": 40,
    "slice_duration": 1,
    "duration": 10,
    "antenna": "RX2"
  }'
```

### 成功响应

```json
{
  "dev_id": "usrp-30B1FDE",
  "status": "IDLE",
  "current_config": {
    "dev_id": "usrp-30B1FDE",
    "freq": 2400000000.0,
    "sample_rate": 1000000.0,
    "bandwidth": 1000000.0,
    "gain": 40.0,
    "slice_duration": 1.0,
    "duration": 10.0,
    "antenna": "RX2"
  },
  "message": "USRP config loaded"
}
```

### 错误

| HTTP 状态 | 原因 |
|-----------|------|
| `400` | 参数超出设备能力、天线无效或带宽大于采样率 |
| `404` | `dev_id` 不存在 |
| `409` | 设备不是 `IDLE`，例如 `OFFLINE` 或 `BUSY` |
| `422` | 缺少必填字段或 JSON 字段类型错误 |

## 8. 启动采集

启动后台采集线程。接口返回 `200` 表示任务已接受，不表示硬件已经完成初始化，也不表示数据文件已经写完。

### 请求

```http
POST /api/usrp/start
Content-Type: application/json
```

```json
{
  "task_id": "task-20260615-001",
  "dev_id": "usrp-30B1FDE",
  "freq": 2400000000,
  "sample_rate": 1000000,
  "bandwidth": 1000000,
  "gain": 40,
  "slice_duration": 1,
  "duration": 10,
  "antenna": "RX2"
}
```

### 参数

参数约束与 `configure` 相同，额外包含：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | 调用方生成的任务 ID，同时作为输出目录名 |

`task_id` 应满足：

- 在业务范围内唯一。
- 建议只使用字母、数字、短横线和下划线。
- 不要包含 `/`、`..` 或其他路径字符。
- 不要复用已有任务 ID，否则文件名相同时可能覆盖旧任务数据。

### curl

```bash
curl -sS -X POST \
  http://10.112.210.4:8100/api/usrp/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task-20260615-001",
    "dev_id": "usrp-30B1FDE",
    "freq": 2400000000,
    "sample_rate": 1000000,
    "bandwidth": 1000000,
    "gain": 40,
    "slice_duration": 1,
    "duration": 10,
    "antenna": "RX2"
  }'
```

### 成功响应

```json
{
  "dev_id": "usrp-30B1FDE",
  "task_id": "task-20260615-001",
  "status": "BUSY",
  "output_dir": "backend/runtime/usrp_tasks/task-20260615-001",
  "message": "USRP acquisition started"
}
```

### 执行过程

1. 接口校验设备必须处于 `IDLE`。
2. 设备立即切换到 `BUSY`。
3. 后台线程初始化 UHD 和 B210。
4. 开始接收 IQ 数据、生成 FFT 并异步压缩切片文件。
5. 达到 `duration`、收到停止信号或发生错误后结束。
6. 等待切片写入完成。
7. 设备恢复为 `IDLE`，`task_id` 变为 `null`。

B210 初始化可能需要数秒。调用方应通过 `GET /api/usrp/devices` 判断任务是否真正完成，不应使用固定 sleep 时间。

### 采样和切片规则

服务按样本数精确控制采集时长：

```text
目标总样本数 = round(sample_rate * duration)
单个完整切片样本数 = round(sample_rate * slice_duration)
```

示例：

```text
sample_rate = 1,000,000
duration = 3
slice_duration = 1
```

结果为 3 个切片，每个切片 1,000,000 个 IQ 样本，总计 3,000,000 个样本。

如果总时长不是切片时长的整数倍，最后一个文件为短切片，其 `slice_duration` 是该文件的实际样本时长。手动停止时也可能生成短切片。

### 持续采集

传入：

```json
{
  "duration": null
}
```

任务会持续运行，直到调用停止接口或采集发生错误。

调用方应发送正数 `duration` 或 `null`。当前实现会把小于等于 0 的 `duration` 视为持续采集，不建议依赖该行为。

### 错误

| HTTP 状态 | 原因 |
|-----------|------|
| `400` | 参数校验失败 |
| `404` | 设备不存在 |
| `409` | 设备不是 `IDLE`；离线或已有任务时均拒绝启动 |
| `422` | 缺少必填字段或字段类型错误 |

后台硬件初始化或采集错误发生在接口返回之后，会记录到服务日志。当前接口没有单独的任务失败查询端点，因此需要结合设备状态、输出文件、日志或平台完成回调判断。

## 9. 输出文件

每个任务的数据保存在服务器：

```text
/home/ue/DeepEM/backend/runtime/usrp_tasks/{task_id}/
```

接口响应中的相对路径为：

```text
backend/runtime/usrp_tasks/{task_id}
```

文件名：

```text
slice_000000.npz
slice_000001.npz
slice_000002.npz
...
```

每个 `.npz` 文件包含：

| 键 | NumPy 类型 | 说明 |
|----|------------|------|
| `iq` | `complex128[]` | 复数 IQ 样本 |
| `freq` | float | 中心频率，Hz |
| `sample_rate` | float | 采样率，Sps |
| `bandwidth` | float | 接收带宽，Hz |
| `gain` | float | 增益，dB |
| `slice_index` | integer | 从 0 开始的切片序号 |
| `slice_duration` | float | 当前文件的实际样本时长，秒 |
| `timestamp` | string | 文件生成时的 UTC 时间 |

Python 读取示例：

```python
import numpy as np

path = "slice_000000.npz"

with np.load(path, allow_pickle=False) as data:
    iq = data["iq"]
    freq = float(data["freq"])
    sample_rate = float(data["sample_rate"])
    duration = float(data["slice_duration"])

print(iq.shape, iq.dtype)
print(freq, sample_rate, duration)
```

远端查看任务文件：

```bash
ssh ue@10.112.210.4
cd /home/ue/DeepEM
find backend/runtime/usrp_tasks/task-20260615-001 \
  -maxdepth 1 -type f -printf '%f %s bytes\n'
```

## 10. 停止采集

向指定设备的当前任务发送停止信号。

### 请求

```http
POST /api/usrp/{dev_id}/stop
Content-Type: application/json
```

```json
{
  "task_id": "task-20260615-001"
}
```

### curl

```bash
curl -sS -X POST \
  http://10.112.210.4:8100/api/usrp/usrp-30B1FDE/stop \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task-20260615-001"
  }'
```

### 成功响应

```json
{
  "dev_id": "usrp-30B1FDE",
  "task_id": "task-20260615-001",
  "status": "IDLE",
  "message": "USRP stop signal sent"
}
```

注意：该响应表示停止信号已发送。响应中的 `status: "IDLE"` 是目标状态，后台线程可能仍需完成当前接收、短切片写入和文件压缩。应继续轮询设备状态，直到：

```json
{
  "status": "IDLE",
  "task_id": null
}
```

### 错误

| HTTP 状态 | 原因 |
|-----------|------|
| `404` | 设备不存在 |
| `409` | 设备不处于 `BUSY`，或请求 `task_id` 与当前任务不一致 |
| `422` | 缺少 `task_id` 或类型错误 |

## 11. WebSocket 实时频谱

### 连接地址

```text
ws://10.112.210.4:8100/api/usrp/{dev_id}/stream
```

当前设备示例：

```text
ws://10.112.210.4:8100/api/usrp/usrp-30B1FDE/stream
```

建议先调用 `scan` 或 `devices` 确认设备存在。对于不存在的设备，当前服务可能接受 WebSocket 连接但不发送初始状态。

### 状态消息

连接成功后，服务立即发送一次当前状态：

```json
{
  "type": "status",
  "dev_id": "usrp-30B1FDE",
  "status": "IDLE",
  "task_id": null,
  "current_config": null
}
```

该状态消息只在连接建立时发送一次。设备之后从 `IDLE` 变为 `BUSY` 或从 `BUSY` 变为 `IDLE` 时，服务当前不会主动再次发送状态消息；状态变化应通过 HTTP 查询。

### FFT 消息

采集期间大约每 100ms 发送一帧：

```json
{
  "type": "fft",
  "dev_id": "usrp-30B1FDE",
  "timestamp": "2026-06-15T08:08:37Z",
  "freq": 2400000000.0,
  "sample_rate": 1000000.0,
  "fft_size": 1024,
  "fft_data": [
    -77.8,
    -76.5,
    -75.2
  ]
}
```

实际 `fft_data` 固定包含 1024 个浮点值。

### FFT 处理方式

| 项目 | 值 |
|------|----|
| FFT 点数 | 1024 |
| 窗函数 | Hanning |
| 幅度公式 | `20 * log10(abs(FFT) + 1e-10)` |
| 排列 | 已执行 `fftshift`，中心频率在数组中间 |
| 目标推送周期 | 约 100ms |

`fft_data` 是未经功率校准的相对 dB 值，不应直接解释为 dBm。

第 `i` 个 FFT 点对应频率可按下式计算：

```text
frequency[i] = freq + (i - fft_size / 2) * sample_rate / fft_size
```

其中 `i` 的范围为 `0` 到 `fft_size - 1`。

### 浏览器示例

```javascript
const devId = "usrp-30B1FDE";
const ws = new WebSocket(
  `ws://10.112.210.4:8100/api/usrp/${devId}/stream`
);

ws.onopen = () => {
  console.log("WebSocket connected");
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);

  if (message.type === "status") {
    console.log("Initial status:", message.status);
    return;
  }

  if (message.type === "fft") {
    console.log(
      message.timestamp,
      message.fft_size,
      message.fft_data.length
    );
    drawWaterfall(message.fft_data);
  }
};

ws.onerror = (event) => {
  console.error("WebSocket error:", event);
};

ws.onclose = () => {
  console.log("WebSocket closed");
};
```

多个客户端可以同时连接同一设备，服务会向所有连接广播相同 FFT 帧。网络调度和客户端处理会造成到达间隔抖动，调用方不应假设每帧严格相隔 100ms。

## 12. 通用错误响应

业务错误通常使用：

```json
{
  "detail": "Device usrp-30B1FDE is OFFLINE, cannot start capture"
}
```

请求模型校验错误使用 HTTP `422`：

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "task_id"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

通用状态码：

| HTTP 状态 | 含义 |
|-----------|------|
| `200` | 请求成功或后台任务已接受 |
| `400` | 参数不符合设备能力或业务约束 |
| `404` | 设备或 HTTP 路径不存在 |
| `409` | 设备状态冲突或任务 ID 不匹配 |
| `422` | 请求 JSON 缺少字段或类型错误 |
| `500` | 未处理的服务端异常 |

## 13. 平台回调

设置 `PLATFORM_BASE_URL` 后，服务会上传每个切片并通知任务结束。

### 上传切片

```http
POST {PLATFORM_BASE_URL}/api/collector/upload
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 任务 ID，即 `task_id` |
| `collector_id` | string | 采集端 ID |
| `file` | file | `.npz` 切片文件 |

文件 MIME 类型为：

```text
application/octet-stream
```

### 完成通知

```http
POST {PLATFORM_BASE_URL}/api/collector/session-complete
Content-Type: application/json
```

```json
{
  "session_id": "task-20260615-001",
  "collector_id": "collector-usrp-001",
  "device_id": "usrp-30B1FDE",
  "status": "completed",
  "message": "capture completed",
  "completed_at": "2026-06-15T08:10:00Z"
}
```

`status` 取值：

| 值 | 含义 |
|----|------|
| `completed` | 达到指定采集时长 |
| `stopped` | 收到手动停止信号 |
| `failed` | 初始化或采集失败 |

回调失败只记录日志，不会改变原 HTTP 接口响应。

## 14. 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `USRP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `USRP_PORT` | `8100` | HTTP 监听端口 |
| `COLLECTOR_ID` | `collector-usrp-001` | 平台回调中的采集端 ID |
| `PLATFORM_BASE_URL` | 空 | 平台地址；为空时禁用上传和完成回调 |

## 15. 服务运维

登录远端：

```bash
ssh ue@10.112.210.4
cd /home/ue/DeepEM
```

启动服务：

```bash
./start_usrp_service.sh
```

停止服务：

```bash
./stop_usrp_service.sh
```

查看服务日志：

```bash
tail -f /tmp/usrp_service.log
```

查看进程：

```bash
pgrep -af 'uv run usrp_service.py|python3 usrp_service.py'
```

查看端口：

```bash
ss -ltnp | grep ':8100'
```

直接检查硬件：

```bash
lsusb
uhd_find_devices
uhd_usrp_probe --args serial=30B1FDE
```

日志中常见信息：

```text
UHD find returned 1 device(s)
Found device: usrp-30B1FDE serial=30B1FDE
Capture started
Duration reached
Capture finished
```

如果出现以下内容，应检查 USB 带宽、采样率和主机负载：

```text
Overflow, continuing...
RX error
Capture error
```

## 16. 推荐调用流程

1. 调用 `POST /api/usrp/scan`。
2. 检查 `scan_error == null`。
3. 找到目标 `dev_id` 并确认 `status == "IDLE"`。
4. 可选调用 `POST /api/usrp/configure` 做参数预校验。
5. 先建立 WebSocket 连接，准备接收 FFT。
6. 调用 `POST /api/usrp/start`。
7. 通过 WebSocket 展示实时频谱。
8. 通过 `GET /api/usrp/devices` 轮询任务状态。
9. 有限时长任务等待自动结束；持续任务调用 `stop`。
10. 等待状态恢复为 `IDLE` 且 `task_id == null`。
11. 读取输出文件，或等待平台完成回调。

## 17. 已验证基线

2026-06-15 在当前 B210 上使用以下参数完成实机验证：

```json
{
  "dev_id": "usrp-30B1FDE",
  "freq": 2400000000,
  "sample_rate": 1000000,
  "bandwidth": 1000000,
  "gain": 40,
  "slice_duration": 1,
  "duration": 3,
  "antenna": "RX2"
}
```

验证结果：

- 扫描返回 `found_count: 1`、设备状态 `IDLE`。
- 启动接口返回 HTTP `200`，设备进入 `BUSY`。
- 3 秒采集生成 3 个 `.npz` 文件。
- 每个文件包含 1,000,000 个 `complex128` IQ 样本。
- 总样本数为 3,000,000。
- WebSocket 收到 30 帧 FFT。
- 每帧包含 1024 个浮点值。
- FFT 平均到达间隔约 103ms。
- 任务结束后设备恢复为 `IDLE`，`task_id` 为 `null`。
- 最终测试未出现 UHD Overflow 或采集错误。

## 18. 注意事项

1. 同一设备同一时间只允许一个采集任务。
2. `start` 必须在设备为 `IDLE` 时调用。
3. `configure` 仅保存和校验参数，不能代替 `start` 请求中的完整参数。
4. `bandwidth` 不能大于 `sample_rate`。
5. 当前 B210 运行在 USB 2.0，高采样率可能导致 Overflow。
6. `start` 返回后仍存在硬件初始化时间，不能把 HTTP 响应时间当作采集起始时间。
7. WebSocket 初始状态只发送一次，任务状态变化应通过 HTTP 查询。
8. FFT 数值是相对 dB，不是校准后的 dBm。
9. `stop` 是异步停止信号，必须继续轮询直到任务真正结束。
10. 使用新的 `task_id`，避免覆盖同名任务目录中的旧切片。
11. 服务重启后内存设备表为空，应先调用一次 `scan`。
