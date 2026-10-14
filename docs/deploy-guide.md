# 部署指南 · 苗绣纹样创作系统

本文档说明如何把系统部署到一台服务器上，供多人（例如一个班级）使用。

**核心原则**：全部装在独立目录，不碰系统环境，卸载就是删目录。

---

## 一、先选部署形态

| 形态 | 适用场景 | 谁需要装环境 |
|---|---|---|
| **A. 单机演示** | 自己电脑上跑通、备课时预览 | 只有你 |
| **B. 服务器部署**（推荐给班级用） | 学生只需浏览器，不用装任何东西 | 只有服务器 |
| **C. 本地 ComfyUI + 远端 Streamlit** | 已有 GPU 机器，但想在别处操作界面 | 两边各装一部分 |

**给全班用就选 B。** 学生端零配置，这是最省事的形态。

---

## 二、形态 B：服务器部署（推荐）

### 目标架构

```
┌─────────────────────────────────────────────┐
│  服务器（Linux + GPU）                       │
│                                              │
│  ┌──────────────┐    ┌───────────────────┐  │
│  │  ComfyUI     │    │  Streamlit        │  │
│  │  127.0.0.1   │◄───│  0.0.0.0:8501     │  │
│  │  :8188       │内部 │  检索/创作/归档    │  │
│  └──────────────┘    └───────────────────┘  │
│         ↑ 不对外         ↑ 对外暴露           │
└─────────────────────────────────────────────┘
                  ▲ 学生浏览器
        http://<服务器IP>:8501
```

**关键点**：ComfyUI 不需要暴露到公网。它和 Streamlit 在同一台机器上，
通过 `127.0.0.1` 内部通信，更安全。

### 前置条件

服务器上需要：

- Linux（Ubuntu 20.04+ / CentOS 7+ 均可）
- Python 3.9 或更高（`python3 --version` 查）
- `python3-venv` 模块（Ubuntu 若无：`sudo apt install python3-venv`）
- ComfyUI 已经在运行（`http://127.0.0.1:8188` 能通）

### 部署步骤

#### 1. 上传项目

```bash
# 在本地打包后上传（scp）
scp miao-system.tar.gz 用户名@服务器IP:~/

# 登录服务器解压
ssh 用户名@服务器IP
tar -xzf miao-system.tar.gz
cd miao-system
```

#### 2. 先做一次检查（不安装任何东西）

```bash
bash deploy_server.sh --dry-run
```

它会检查：Python 版本、venv 模块、磁盘空间、端口占用、ComfyUI 连通性。
**看清楚有没有警告再继续。**

#### 3. 正式部署

```bash
bash deploy_server.sh
```

脚本会做这些事（每步都有输出）：

1. 环境检查
2. 复制文件到 `~/miao-system/`（若已在目标目录则跳过）
3. 创建独立 venv：`~/miao-system/venv`
4. 安装依赖（默认用清华镜像加速）
5. 运行环境自检
6. 生成 `run.sh` / `stop.sh`

**全程不需要 root，不修改系统任何文件。**

#### 4. 启动服务

```bash
# 前台运行（能看到日志，Ctrl+C 停止）
bash ~/miao-system/run.sh

# 或后台运行（关掉终端也不断）
nohup bash ~/miao-system/run.sh > ~/miao-system/app.log 2>&1 &
```

#### 5. 放行端口

⚠️ **这一步要你自己在云服务商控制台操作，脚本不会自动改防火墙。**

腾讯云 / 阿里云 / 华为云：进入「安全组」→ 添加入站规则 → 放行 TCP `8501`。

放行后学生访问：`http://<服务器公网IP>:8501`

**不要放行 8188**——ComfyUI 不需要对外。

---

## 三、图片数据怎么放

系统需要纹样图片。三种方式，任选其一：

**方式一（推荐）**：放进项目目录

```bash
mkdir -p ~/miao-system/data/images
# 把图片拷进去
cp /path/to/your/images/*.jpg ~/miao-system/data/images/
```

**方式二**：设置环境变量

```bash
export MIAO_IMAGE_ROOT=/path/to/images
```

要永久生效，写进 `~/.bashrc`。

**方式三**：在界面左侧栏「图片所在文件夹」里现场填写。

### 首次使用要先生成元数据

```bash
cd ~/miao-system
./venv/bin/python build_metadata.py --src "图片目录" --out metadata/images.csv
```

看到类似输出就成功了：

```
扫描到 1000 张图片
变体分组：basic 250 / texture 250 / color 250 / mixup 250
已写入 metadata/images.csv
```

> ⚠️ **注意**：随包附带的 `metadata/images.csv` 是基于原作者的数据集生成的，
> 里面的 `file_path` 字段指向原作者机器的路径。**换机器后必须重新生成**，
> 否则图片显示不出来。

---

## 四、环境变量一览

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `IMAGE_API_KEY` | 空 | 免费云端 API 密钥（可选，见 `docs/free-api-guide.md`） |
| `IMAGE_API_MODEL` | `Qwen/Qwen-Image-Edit` | 云端生图模型 |
| `MIAO_IMAGE_ROOT` | 自动探测 | 纹样图片目录 |
| `NEO4J_URI` | `bolt://localhost:7687` | 图数据库地址（可选） |
| `NEO4J_USER` | `neo4j` | 图数据库用户 |
| `NEO4J_PASSWORD` | 空 | 图数据库密码 |

**这三个外部服务都不是必需的**，缺哪个自动降级：

| 缺什么 | 影响 |
|---|---|
| Neo4j | 检索自动读 CSV，**功能不变** |
| ComfyUI | 尝试云端 API，**都没有则不能生成** |
| 云端 API | 同上 |

---

## 五、日常运维

### 查看日志

```bash
tail -f ~/miao-system/app.log
```

### 重启服务

```bash
bash ~/miao-system/stop.sh
nohup bash ~/miao-system/run.sh > ~/miao-system/app.log 2>&1 &
```

### 更新代码

重新上传后重新跑一次部署脚本即可，venv 会复用，很快：

```bash
bash deploy_server.sh
```

### 开机自启（可选）

```bash
crontab -e
# 加入这一行：
@reboot sleep 30 && nohup bash $HOME/miao-system/run.sh > $HOME/miao-system/app.log 2>&1 &
```

### 完全卸载

```bash
bash ~/miao-system/stop.sh
rm -rf ~/miao-system
```

**服务器即恢复原样**，不影响系统 Python、不影响 ComfyUI、不影响其他任何东西。

---

## 六、给别人用之前必须做的三件事

如果你要把这套系统交给别人（同事、助教、其他老师），**务必先做这三件事**：

### 1. 加访问密码

系统默认**没有任何登录保护**——任何知道网址的人都能用，还能看到所有学生的作品。

Streamlit 自带一个简单的密码保护，创建 `~/.streamlit/credentials.toml`：

```bash
mkdir -p ~/.streamlit
cat > ~/.streamlit/credentials.toml <<'EOF'
[general]
email = ""
EOF
```

更稳妥的做法是用反向代理（nginx）加 HTTP Basic 认证，或改用支持多用户的框架。

**正式开课前一定要处理这个问题。**

### 2. 隐私提醒

用云端 API 时，学生选中的素材会上传到第三方服务器。
如果素材涉及未公开内容或有版权约定，要在课程说明里告知。
本地 ComfyUI 没有这个问题。

### 3. 清理个人数据

打包分发前确认：

- 没有明文密码、API 密钥
- 没有指向你个人电脑的绝对路径
- `metadata/works.csv`（学生作品记录）是否要一起给

本项目已做过清理，仓库中不含任何个人凭据。

---

## 七、常见问题

### 端口被占用

```bash
bash deploy_server.sh --port 8502
```

### 学生访问不了

按顺序排查：

1. 服务器上服务在跑吗？`ss -tln | grep 8501`
2. 本机能访问吗？`curl http://127.0.0.1:8501`
3. 云服务商安全组放行了吗？（最常见的原因）
4. 服务器系统防火墙放行了吗？`sudo ufw status`

### ComfyUI 连不上

```bash
# 看它在不在跑
ps aux | grep comfy | grep -v grep

# 看端口
ss -tln | grep 8188

# 手动测
curl http://127.0.0.1:8188/system_stats
```

如果 ComfyUI 在**另一台机器**上，`COMFYUI_URL` 要填那台的局域网 IP。
注意：若服务器配了 HTTP 代理，连内网地址可能被代理拦截，
系统已对私有网段做了绕过处理，但仍建议确认一下。

### 出图很慢

- 检查 GPU 占用：`nvidia-smi`
- 如果多人在用同一张卡，可能显存不足
- 降低分辨率，或在 ComfyUI 启动参数里设显存上限

### 图片显示不出来

`metadata/images.csv` 里的 `file_path` 是旧机器的路径。
重新生成：

```bash
cd ~/miao-system
./venv/bin/python build_metadata.py --src "图片目录" --out metadata/images.csv
```

---

## 八、安全清单

部署完成后逐项确认：

- [ ] 已加访问密码（**开课前必做**）
- [ ] 只放行了 8501，**没有**放行 8188
- [ ] 服务器 SSH 密码已改成强密码（不要用默认密码）
- [ ] 没有把密码/密钥提交到版本库
- [ ] 确认了素材版权与隐私边界
- [ ] 知道怎么停止和卸载（`stop.sh` + `rm -rf`）
