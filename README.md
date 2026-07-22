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
