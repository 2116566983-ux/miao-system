# 连接远端 ComfyUI（SSH 隧道）

本文档说明如何让系统使用**另一台机器上已经跑起来的 ComfyUI**（例如学校的 GPU 服务器），
而不需要在本地装显卡或模型。

**适合的场景**：实验室/学校有一台带 GPU 的服务器，上面已经跑着 ComfyUI。
你只想用自己的笔记本操作界面。

---

## 一、为什么走 SSH 隧道

服务器上的 ComfyUI 通常是这两种情况之一：

| 情况 | 现象 | 直接连的后果 |
|---|---|---|
| 只监听 `127.0.0.1` | 外网完全连不上 | 需要改 ComfyUI 启动参数 |
| 监听 `0.0.0.0` 但云安全组没放行 | 外网连不上 | 需要开放安全组端口 |
| 监听 `0.0.0.0` 且放行了 | 能连上 | **公网可访问，任何人能调你的 GPU** |

**SSH 隧道的好处**：

- ComfyUI 完全不用改配置、不用重启
- 不用开公网端口，不会被人白嫖 GPU
- 隧道随进程关闭而断开，用完即止

---

## 二、配置（三分钟）

设好这四个环境变量，系统会自动建隧道，不需要手工敲 `ssh -L`：

**Windows**

```bat
set COMFYUI_SSH_HOST=你的服务器地址
set COMFYUI_SSH_PORT=22
set COMFYUI_SSH_USER=你的用户名
set COMFYUI_SSH_PASSWORD=你的密码
```

**Linux / macOS**

```bash
export COMFYUI_SSH_HOST=你的服务器地址
export COMFYUI_SSH_PORT=22
export COMFYUI_SSH_USER=你的用户名
export COMFYUI_SSH_PASSWORD=你的密码
```

要永久生效，Windows 用 `setx`，Linux 写进 `~/.bashrc`。

### 用密钥代替密码（推荐）

密码写在环境变量里毕竟不安全。改用密钥：

```bash
export COMFYUI_SSH_HOST=你的服务器地址
export COMFYUI_SSH_USER=你的用户名
export COMFYUI_SSH_KEY=/path/to/id_rsa     # 私钥路径
```

设了 `COMFYUI_SSH_KEY` 就不用 `COMFYUI_SSH_PASSWORD`。

### 完整环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `COMFYUI_SSH_HOST` | ✅ | — | 服务器地址（设了才启用隧道） |
| `COMFYUI_SSH_PORT` | | `22` | SSH 端口 |
| `COMFYUI_SSH_USER` | ✅ | — | SSH 用户名 |
| `COMFYUI_SSH_PASSWORD` | 二选一 | — | SSH 密码 |
| `COMFYUI_SSH_KEY` | 二选一 | — | SSH 私钥路径（更推荐） |
| `COMFYUI_REMOTE` | | `127.0.0.1:8188` | ComfyUI 在**服务器上**的地址 |

> ⚠️ 不要设 `COMFYUI_URL`。设了 `COMFYUI_SSH_HOST` 后，
> 系统会自动把地址指向隧道的本地端口。

---

## 三、验证

```bash
python check_env.py
```

看到这样就是通了：

```
[后端 A] 远端 ComfyUI（走 SSH 隧道）
  服务器：gpu.example.edu:22
  [可用] 连接成功
     运行设备：NVIDIA A100-PCIE-40GB · 39.5GB
     隧道：gpu.example.edu:22 -> 127.0.0.1:8188
     模型盘点：整合检查点 0 · 扩散模型 3 · 文本编码器 2 · VAE 5
     [可用] 可以直接出图（分离式模型）
```

### 只验证链路（不发真实任务）

```bash
python tools/verify_comfy.py
```

它用一个纯图像处理工作流（不需要模型），完整验证
`上传 → 提交 → 轮询 → 取回` 四个环节。

### 做一次真实出图

```bash
python tools/test_generate.py --steps 12 --size 768
```

> 该脚本会先检查队列，**有人在用就不提交**，避免抢 GPU。

---

## 四、关键前提：服务器上必须有生成模型

**这是最容易踩的坑。** ComfyUI 能连上 ≠ 能出图。

ComfyUI 有两套加载方式，模型放在不同目录：

| 方式 | 加载节点 | 模型目录 | 典型文件 |
|---|---|---|---|
| 整合检查点 | `CheckpointLoaderSimple` | `models/checkpoints/` | `sd_xl_base.safetensors` |
| 分离式 | `UNETLoader` + `CLIPLoader` + `VAELoader` | `models/diffusion_models/` `models/text_encoders/` `models/vae/` | UNet + CLIP + VAE 三个文件 |

**两套都没有 → 出不了图。**

系统的处理方式：连上后自动盘点模型，并**自动挑选匹配的工作流模板**：

| 服务器情况 | 自动选用 |
|---|---|
| 有整合检查点 | `workflows/img2img_template.json` |
| 无检查点，但分离式齐全 | `workflows/img2img_qwen.json` |
| 都不全 | 提示缺什么，并建议改用云端 API |

界面的「高级参数（教师用）」里会显示自动选择的结果和理由。

### 实测遇到过的情况

某台 A100 服务器上是视频生成环境（MiniMax H3），
`models/checkpoints/` 是**空的**，但 `diffusion_models/` 里有一个图片模型
`anima-preview3-base.safetensors`，配套 `qwen_3_06b_base.safetensors`
文本编码器和 `qwen_image_vae.safetensors`。

这种情况就是「分离式模型」，用 `img2img_qwen.json` 可以正常出图
（实测：768×768、12 步约 90 秒含模型加载，模型热了之后 6 秒）。

---

## 五、换模型怎么办

模板文件里的模型名是写死的，换模型改三处：

```json
"1": { "class_type": "UNETLoader",
       "inputs": { "unet_name": "你的扩散模型.safetensors", "weight_dtype": "default" } },
"2": { "class_type": "CLIPLoader",
       "inputs": { "clip_name": "你的文本编码器.safetensors", "type": "qwen_image" } },
"3": { "class_type": "VAELoader",
       "inputs": { "vae_name": "你的VAE.safetensors" } }
```

`type` 字段要跟模型家族对应（`qwen_image` / `flux` / `sd3` / `wan` 等），
可选项由 ComfyUI 提供，查法：

```bash
python -c "
import sys; sys.path.insert(0,'app')
from comfy_client import client_from_env
c = client_from_env()
print(c.node_options('CLIPLoader','type'))
print(c.node_options('UNETLoader','unet_name'))
print(c.node_options('VAELoader','vae_name'))
"
```

### 在模板里写注释

模板 JSON 里可以加 `_readme`、`_comment` 之类的说明键，
**提交时会自动剔除**，不会污染 ComfyUI。

> 这条是有代价换来的：ComfyUI 会把顶层任意键当节点解析，
> 遇到说明性键会抛未捕获异常，只返回一个 **500**，
> 看不出任何原因。所以系统在读取和提交两处都做了清洗。

---

## 六、共享机器上的资源占用（重要）

如果服务器不是你独占的，用之前最好心里有数：**这个系统会占多少资源、会不会影响别人。**

### 实测占用（单次生成）

| 项目 | 占用 | 说明 |
|---|---|---|
| 显存 | 约 6 GB（首次加载后常驻） | 4GB 模型 + 1.2GB 编码器 + VAE |
| 单次出图算力 | 6–90 秒 | 模型已热约 6–7 秒；首次含加载约 90 秒 |
| 磁盘（输入图） | 约 100 KB / 张 | 上传的素材 |
| 磁盘（输出图） | 约 0.5–1.5 MB / 张 | 生成的 PNG |
| 内存 | 约 7–8 GB（ComfyUI 进程本身） | ComfyUI 启动就占，与出图次数无关 |

**对比参考**：一张 A100-40GB 卡上，这个系统用掉约 **15%**。

### 用完请归还显存

ComfyUI 默认把模型**常驻显存**，方便下次快速出图
（所以第一张 86 秒、后面 7 秒）。

但别人要用这张卡时，你那几 GB 就是挡路的。**跑完调一次释放**：

```python
from comfy_client import client_from_env
c = client_from_env()
c.free_memory()          # 卸载模型 + 释放显存
```

或者命令行：

```bash
python -c "
import sys; sys.path.insert(0,'app')
from comfy_client import client_from_env
print('已释放' if client_from_env().free_memory() else '失败')
"
```

实测效果：ComfyUI 在 GPU 0 的占用从 **5974 MiB 降到 566 MiB**，归还约 5.4 GB。

> 这只影响缓存，**不删任何文件、不改任何配置**。
> 下次提交任务时 ComfyUI 会自动重新加载模型（慢一点，但公平）。

### 提交前先看队列

多人共用的机器上，直接提交会插队。先查一下：

```python
q = client_from_env().queue_position()
# {'running': 0, 'pending': 0} 表示空闲
```

本项目自带的 `tools/test_generate.py` **已经内置这个检查**：
有人在跑就自动放弃提交。

### 全班使用时要注意的

40 个学生同时点「生成」时：

- **ComfyUI 是串行处理的**（默认一次跑一个任务），请求会排队
- 每张约 6–7 秒（模型已热），40 张约 5 分钟排完 —— 可接受
- 但**排队期间显存一直被占着**，会持续挤压同卡上的其他人
- **输出图片会堆在 ComfyUI 的 `output/` 目录**，多人共用时会和别人的混在一起

**建议**：

1. 跟机器管理员/其他使用者**提前打招呼**，说明大概什么时段用、用多久
2. 尽量**避开别人的训练任务时段**（训练会长时间占满 GPU）
3. 给输出目录加前缀区分（模板里 `filename_prefix` 字段）
4. 课后及时清理自己的输出和上传文件

### 我做过的事（供你判断）

以本项目在服务器上的实际操作为例：

- **只调用 HTTP API** —— 提交任务、查询状态、下载结果
- **建立 SSH 隧道** —— 不开放公网端口，用完即断
- **没有修改任何文件**、没有改配置、没有装东西、没有重启服务
- **没有读别人的私有文件**
- 全部排查用的命令都是只读的（`ls` / `ps` / `df` / `nvidia-smi`）
- 生成的产物落在 ComfyUI 自己的 `input/` `output/` 目录（这是它正常的运行方式）

累计磁盘痕迹约 **2.9 MB**，显存已释放。

---

## 七、常见问题

### 连不上：认证失败

- 密码/密钥是否正确
- 用户名是否正确（有些服务器要 `user@domain`）
- 服务器是否限制了来源 IP

### 连不上：端口不通

```bash
# 本机测 SSH 端口
telnet 你的服务器 22
# 或
nc -zv 你的服务器 22
```

不通说明网络或防火墙问题，跟本系统无关。

### 提示"未安装 paramiko"

隧道功能需要 paramiko：

```bash
pip install paramiko
```

不用隧道的话不需要装它——这是可选依赖。

### 提交后返回 500

ComfyUI 的 500 通常没有可读信息。按顺序排查：

1. 工作流里有没有非节点键（系统已自动清洗，但自定义模板要留意）
2. 模型名是否与服务器上的实际文件一致
3. 显存是否够（看 `nvidia-smi`）
4. 节点参数是否与该模型家族匹配（如 `CLIPLoader.type`）

### 出图很慢

- **首次加载模型慢**：4GB 模型加载要 1–2 分钟，之后就快了
- **模型热了之后**：768×768、12 步约 6 秒（A100）
- 如果一直慢，检查是不是在排队（`comfy.queue_position()`）

### 想确认服务器上有没有人在用

```bash
python -c "
import sys; sys.path.insert(0,'app')
from comfy_client import client_from_env
c = client_from_env()
print('队列：', c.queue_position())
"
```

`running` 和 `pending` 都是 0 就是空闲。

---

## 八、顺手提醒

- **只在获得授权的机器上使用。** 如果服务器是多人共用，先确认清楚，别抢占别人的 GPU。
- **别把密码写进代码或提交到版本库。** 用环境变量或密钥文件。
- **公共服务器上注意输出文件的位置。** 生成的图片会落在 ComfyUI 自己的 `output/` 目录里，
  多人共用时会混在一起。
- 本系统提交任务前会**先查队列**，有人在跑就不提交（见 `tools/test_generate.py`）。
