# 快速启动

```bash
# 1) 进入项目目录
cd /DeepEM_main

# 2) 创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate

# 3) 安装平台依赖
pip install -e .

# 4) 配置主平台与 USRP 采集服务
export DEEPEM_LLM_API_KEY="null"
export DEEPEM_LLM_BASE_URL="http://localhost:8001/v1"
export DEEPEM_LLM_MODEL="qwen36_35b_a3b"
export DEEPEM_USRP_BASE_URL="http://10.112.210.4:8100"

# 5) 启动平台
python -m uvicorn main.server:app --host 0.0.0.0 --port 8961 --reload
```



## 功能简介

目前平台包含真实 USRP 采集接入、研判智能体和问答智能体，用于支撑异常无线信号的自动采集、证据落库、复核研判与任务查询。

### 研判智能体

研判智能体面向异常信号的自动化分析流程。平台点击启动采集后会按 USRP 扫频计划调用设备平台，采集端回传 `.npz` 分片后，主平台会完成信号解析、特征提取、时频图生成、证据元信息落库和异常判定。对于可疑信号，研判智能体会结合场所基线、USRP 任务状态、近期观测记录和工具调用结果，对信号进行复核分析，判断其异常程度、可能原因和处置优先级，并生成或更新异常 Case，辅助操作员完成研判闭环。

### 问答智能体

问答智能体面向操作员的人机交互与任务查询。操作员可以通过聊天界面使用自然语言查询当前采集状态、历史信号、异常 Case、观测记录和分析结果。系统还集成 NL2SQL 能力，支持基于默认或上传的 SQLite 数据库进行只读查询，将自然语言问题转换为 SQL 并返回结构化结果。
