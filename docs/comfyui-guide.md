# ComfyUI 安装与工作流制作指南

面向本项目（苗绣纹样 AI 辅助重组）。按顺序执行即可。

你的硬件：**NVIDIA GeForce RTX 4060 Laptop · 8GB 显存** —— 足够跑 SDXL 级别模型。

---

## 一、安装 ComfyUI

### 方式 A：ComfyUI Desktop（推荐，图形界面）

1. 打开 https://www.comfy.org/download
2. 下载 Windows 版安装包，双击安装
3. 首次启动会让你选择模型存放目录，建议选空间大的盘（至少留 30GB）
4. 安装完成后会自动打开浏览器界面，地址形如 `http://127.0.0.1:8188`

### 方式 B：手动部署（适合想完全掌控的）

```bash
# 1. 克隆仓库
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI

# 2. 创建虚拟环境（Python 3.10+）
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖（CUDA 12.1 版本，适配 40 系显卡）
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt

# 4. 启动
python main.py
```

启动后访问 `http://127.0.0.1:8188`。

---

## 二、下载模型

**没有模型，ComfyUI 无法生成任何东西。** 这是最容易卡住的一步。

### 需要下载的

| 类型 | 推荐模型 | 大小 | 下载位置 |
|---|---|---|---|
| 主模型 | SDXL Base 1.0 | ~6.5GB | `models/checkpoints/` |
| VAE（可选） | SDXL VAE | ~335MB | `models/vae/` |

### 下载渠道

- **Hugging Face**：https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0
  下载 `sd_xl_base_1.0.safetensors`，放入 `models/checkpoints/`
- **国内镜像**（如果 HF 访问慢）：https://hf-mirror.com/
- **ModelScope**：https://modelscope.cn/ （国内速度好）

> **8GB 显存的注意**：SDXL 在 8GB 上可以跑，但分辨率建议控制在 **1024×1024** 以内。
> 如果爆显存，启动时加 `--lowvram` 参数：
> ```bash
> python main.py --lowvram
> ```

### 验证模型就位

重启 ComfyUI，在界面里添加 `Load Checkpoint` 节点，
下拉框能看到 `sd_xl_base_1.0.safetensors` 就对了。

---

## 三、制作工作流

项目已经**内置两套可直接用的模板**，你可以先用它们跑通，
再用 ComfyUI 界面替换成自己调好的版本。

### 3.1 直接用内置模板（快速验证）

内置模板文件：

```
workflows/img2img_template.json          单图重组
workflows/multi_motif_template.json      多纹样组合
```

**重要**：内置模板里的模型名是 `sd_xl_base_1.0.safetensors`。
如果你下载的模型文件名不同，需要改：

打开 `app/workflow.py`，找到 `minimal_img2img()` 函数，
修改 `checkpoint_name` 参数默认值，或直接改 JSON 文件里的 `ckpt_name` 字段。

### 3.2 自己制作工作流（推荐）

1. 在 ComfyUI 界面里搭好流程：
   - `Load Checkpoint` → 加载模型
   - `Load Image` → 加载纹样图片
   - `VAE Encode` → 图片转 latent
   - `CLIP Text Encode` → 正/负向提示词（**两个**）
   - `KSampler` → 采样（`denoise` 设为 0.55 左右做图生图）
   - `VAE Decode` → latent 转图片
   - `Save Image` → 保存

2. **关键步骤**：把需要由程序控制的输入改成占位符。选中节点后右键 →
   `Convert widget to input`，或直接在导出的 JSON 里改。

   占位符对照表：

   | 占位符 | 填在哪个输入 | 说明 |
   |---|---|---|
   | `__INPUT_IMAGE__` | `LoadImage.image` | 主纹样（程序自动上传替换） |
   | `__INPUT_IMAGE_2__` | 第二个 `LoadImage.image` | 多纹样组合的第二张 |
   | `__POSITIVE__` | 正向 `CLIPTextEncode.text` | 学生可改 |
   | `__NEGATIVE__` | 负向 `CLIPTextEncode.text` | 教师设定 |
   | `__SEED__` | `KSampler.seed` | 随机种子 |
   | `__DENOISE__` | `KSampler.denoise` | 创作强度 |
   | `__BLEND__` | `ImageBlend.blend_factor` | 融合比例 |

3. **导出 API 格式**：菜单 → `Workflow` → `Export (API)`
   ⚠️ 必须导 **API Format**，普通格式程序无法识别。

4. 把导出的 JSON 覆盖到：
   ```
   workflows/img2img_template.json          或
   workflows/multi_motif_template.json
   ```

### 3.3 多纹样组合的额外要求

`multi_motif_template.json` 需要两个 `LoadImage` 节点，
以及一个 `ImageBlend` 节点把两张图融合。结构：

```
LoadImage(主纹样) ──┐
                    ├─→ ImageBlend → VAE Encode → KSampler → ...
LoadImage(纹样2)  ──┘
```

如果不用 `ImageBlend`，也可以改成用 `ImageCompositeMasked` 或
`LatentComposite`，只要保留 `__BLEND__` 占位符即可（没有就删掉）。

---

## 四、接入本项目

### 1. 确认 ComfyUI 在跑

```bash
curl http://127.0.0.1:8188/system_stats
```

能返回 JSON 就说明通了。

### 2. 设置环境变量并启动系统

```bash
cd miao-system

export COMFYUI_URL="http://127.0.0.1:8188"
streamlit run app/streamlit_app.py
```

界面右上角徽标会从「生成未连接」变成「**生成可用 · 设备名**」。

### 3. 完整流程

1. 「① 检索纹样」→ 选 1–2 张 → **加入我的选择**
2. 「② AI 重组创作」→ 填学号 → 选模式 → 调强度 → **开始生成**
3. 点 **查询进度 / 取回结果**（ComfyUI 是异步的，需要手动查或刷新）
4. 生成完成后自动归档，记录提示词、种子、来源素材
5. 「③ 我的作品」查看，教师可审核

### 4. 如果 ComfyUI 在另一台机器

比如部署在教师的服务器上：

```bash
export COMFYUI_URL="http://192.168.1.100:8188"
```

ComfyUI 需允许外部访问：

```bash
python main.py --listen 0.0.0.0 --port 8188
```

⚠️ **`--listen 0.0.0.0` 会把服务暴露到网络**。校内使用要在防火墙限制来源 IP，
不要直接开到公网。

---

## 五、常见问题

**Q：界面显示「生成未连接」**
A：依次检查——ComfyUI 是否在运行；端口是否 8188；`COMFYUI_URL` 是否正确；
防火墙是否拦截；如果配了系统代理，可能需要加 `NO_PROXY=127.0.0.1`。

**Q：提交任务返回「工作流被拒绝」**
A：九成是**模板里的模型名对不上**。检查 JSON 里 `ckpt_name` 字段与实际模型文件名是否一致。

**Q：生成很慢**
A：4060 Laptop 跑 SDXL 约 15–30 秒一张（1024×1024、24 步）。
课堂上建议降到 768×768、20 步，或让教师端批量预生成。

**Q：显存不足（CUDA out of memory）**
A：启动加 `--lowvram`；降低分辨率；减少 `steps`；关掉其他占显存的程序。

**Q：多人同时提交会不会冲突**
A：ComfyUI 自带队列，会依次处理。界面上「查询进度」会显示队列里运行和等待的数量。
课堂场景建议**分批提交**，或者教师端统一收集后批量跑。

---

## 六、课堂部署建议（对应方案里的分层设计）

| 环节 | 部署位置 | 学生操作 |
|---|---|---|
| ComfyUI + 模型 | **教师端一台 GPU 机器** | 无需安装任何东西 |
| Streamlit 检索 | 同上或另一台内网机器 | 浏览器访问 |
| 学生电脑 | 无要求 | 只开浏览器 |

这样做的价值：**学生不用装环境、不用下模型（省 6.5GB×人数）**，
且工作流由教师预先验证固定，避免课堂现场出问题。

符合方案里「AI 图像生成集中部署在一台具备 GPU 的教师端服务器上，
学生通过浏览器提交任务」的设想。
