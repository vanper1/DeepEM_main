# 快速启动

```bash
# 1) 进入项目目录
cd /home/deepem/DeepEM_main2
source .venv/bin/activate
export DEEPEM_LLM_API_KEY="null"
export DEEPEM_LLM_BASE_URL="http://localhost:6000/v1"
export DEEPEM_LLM_MODEL="qwen36_35B_A3B"
export DEEPEM_USRP_BASE_URL="http://127.0.0.1:8901"
python -m uvicorn main.server:app --host 0.0.0.0 --port 8146




# 2) 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 3) 安装平台依赖
pip install -e .


# vllm启动模型
```bash

cd /home/deepem/LLMs && /home/deepem/LLMs/.venv/bin/python3 .venv/bin/vllm serve Qwen3.6-35B-A3B-FP8 --served-model-name qwen36_35B_A3B --trust-remote-code --host 0.0.0.0 --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.6 --limit-mm-per-prompt '{"image": 4}' --max-num-seqs 1 --max-num-batched-tokens 32768 --moe-backend triton --enforce-eager --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 --reasoning-config '{"reasoning_start_str": "<think>", "reasoning_end_str": "I have to give the final answer.</think>"}' --speculative-config '{"method":"dflash","model":"/home/deepem/LLMs/Qwen3.6-35B-A3B-DFlash","num_speculative_tokens":15}' --attention-backend flash_attn

cd /home/deepem/LLMs && /home/deepem/LLMs/.venv/bin/python3 .venv/bin/vllm serve Qwen3.6-35B-A3B-FP8 --served-model-name qwen36_35B_A3B --trust-remote-code --host 0.0.0.0 --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.6 --limit-mm-per-prompt '{"image": 4}' --max-num-seqs 1 --max-num-batched-tokens 32768 --moe-backend triton --enforce-eager --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 --reasoning-config '{"reasoning_start_str": "<think>", "reasoning_end_str": "我需要做最终回答.</think>"}'  --attention-backend flash_attn

cd /home/deepem/LLMs && vllm serve /home/deepem/LLMs/Qwen3.6-35B-A3B-FP8 --served-model-name qwen36_35B_A3B --trust-remote-code --host 0.0.0.0 --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.6 --limit-mm-per-prompt '{"image": 4}' --max-num-seqs 1 --max-num-batched-tokens 32768 --moe-backend triton --enforce-eager --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 --reasoning-config '{"reasoning_start_str": "", "reasoning_end_str": "思考预算用尽，下面我需要做最终回答."}' --speculative-config '{"method":"dflash","model":"/home/deepem/LLMs/Qwen3.6-35B-A3B-DFlash","num_speculative_tokens":15}' --attention-backend flash_attn


```
# 查看日志：tail -f vllm.log 
# 创建tmux new -s vllm
# 查看vllm终端日志，连接 tmux attach -t vllm
# sudo systemctl start myplatform 启动平台
# journalctl -u myplatform -f 查看平台日志
# sudo systemctl stop myplatform 终止平台


