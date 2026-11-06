"""FinchAI-Rune2 融合脚本：LoRA 并进 LFM3 基座，出 bf16 完整权重

注意：LFM3 走 trust_remote_code，仓库里那三个 .py 必须一起带上，
否则别人 from_pretrained 会直接报 ModuleNotFoundError。
"""
import os, glob, shutil, time

for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(k, None)
os.environ['HF_HUB_OFFLINE'] = '1'

BASE = '/Users/hwc/LycheeAI/LFM3-1.7B'
ADAPTER = '/Users/hwc/LycheeAI/adapters-rune2-v2'
OUT = '/Users/hwc/LycheeAI/FinchAI-Rune2-f16'

if os.path.isdir(OUT) and glob.glob(OUT + '/*'):
    raise SystemExit(f'拒绝覆盖已有产物：{OUT}')

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

t0 = time.time()
print('加载基座（bf16，CPU）...', flush=True)
base = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.bfloat16,
                                            trust_remote_code=True,
                                            low_cpu_mem_usage=True)
print(f'  {time.time()-t0:.1f}s', flush=True)

print('合并 LoRA ...', flush=True)
model = PeftModel.from_pretrained(base, ADAPTER).merge_and_unload()
print(f'  已合并（{time.time()-t0:.1f}s）', flush=True)

print(f'保存到 {OUT} ...', flush=True)
model.save_pretrained(OUT, safe_serialization=True)
AutoTokenizer.from_pretrained(BASE, trust_remote_code=True).save_pretrained(OUT)

# trust_remote_code 依赖的三个文件必须一起走
copied = []
for f in ('configuration_lfm3.py', 'modeling_lfm3.py', 'lfm3_conv.py'):
    src = os.path.join(BASE, f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(OUT, f))
        copied.append(f)

files = sorted(os.path.basename(p) for p in glob.glob(OUT + '/*'))
tot = sum(os.path.getsize(os.path.join(OUT, f)) for f in files if os.path.isfile(os.path.join(OUT, f)))
print(f'  文件 {len(files)} 个：{files}', flush=True)
print(f'  补入 modeling 文件：{copied}', flush=True)
print(f'  合计 {tot/2**30:.2f} GiB', flush=True)
print(f'\n✓ 融合完成，用时 {(time.time()-t0)/60:.1f} 分钟', flush=True)
