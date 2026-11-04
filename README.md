# kg-notebook

把 Kaggle 上的 ComfyUI 笔记本拆成 **可复用的库 `kgcomfy`** + **每个笔记本一份 YAML 配置**。

原来的 `tts-img.ipynb` 把安装、更新、模型软链、frp、filebrowser、启动全部写死在单元格里，
换一个模型/笔记本就要复制粘贴一遍。现在：

- 公共逻辑只在 `src/kgcomfy/` 维护一次；
- 模型路径、更新开关、frp 地址/子域名、密钥名等全部进入 `src/kgcomfy/configs/*.yaml`；
- 笔记本本身只保留“加载库 → 覆盖少量配置 → 调用方法”。

## 目录结构

```
tts-img.ipynb                     # 重构后的薄笔记本
legacy/tts-img.original.ipynb     # 原笔记本备份（已抹掉硬编码 token）
pyproject.toml
src/kgcomfy/
  app.py                          # KaggleComfy 门面：install/update/link/start/run
  config.py                       # 类型化配置 + YAML/JSON 加载 + 校验
  shell.py                        # 命令执行、aria2 下载、解压、软链、安全删除
  secrets.py                      # Kaggle Secrets / 环境变量 / 显式覆盖
  comfyui.py                      # ComfyUI 与便携 Python 运行时、自定义节点
  supervisor.py                   # ComfyUI 看门狗：健康检查、重启、日志/状态
  models.py                       # 模型路径映射、清理
  frp.py                          # frpc 安装、TOML 生成、后台启动
  filebrowser.py                  # filebrowser 安装、YAML 生成、后台启动
  publish.py                      # 可选：打包并上传 ModelScope
  cli.py                          # kgcomfy show-config / prepare / run
  configs/tts-img.yaml            # tts-img 默认配置
tests/                            # 标准库 unittest，无需额外依赖
```

## 在 Kaggle 上使用

### 方式 A：仓库作为 Kaggle Dataset（推荐，离线可用）

1. 把本仓库上传为私有 Dataset，名称建议 `kg-notebook-lib`。
2. 在笔记本右侧 **Add-ons → Secrets** 添加：
   - `frp_token`（必需）
   - `modelscope_token`（仅上传模型/整包时需要）
3. 把 Dataset 挂载到笔记本，然后运行 `tts-img.ipynb` 的第一个 Cell。
   它会自动把 `/kaggle/input/kg-notebook-lib/src` 加入 `sys.path`。

### 方式 B：pip 从 Git 仓库安装

```python
!pip install -q "git+https://github.com/<your-account>/kg-notebook.git"
import kgcomfy
```

`configs/*.yaml` 会随包一起安装，`load_config("tts-img.yaml")` 仍可直接使用。
笔记本的 bootstrap Cell 已经兼容这种方式（先尝试 `import kgcomfy`）。

## 笔记本里的用法

```python
from kgcomfy import KaggleComfy, load_config

cfg = load_config(
    "tts-img.yaml",
    overrides={
        "updates": {"comfyui": False, "custom_nodes": False},
        "frp": {"subdomain": "tts", "server_addr": "oracle2.120168.xyz"},
    },
)
app = KaggleComfy(cfg)

app.install()        # 下载/解压运行时 + 补装缺失节点
app.update()         # 受 cfg.updates 控制
app.cleanup()        # 受 cfg.cleanup.enabled 控制
app.link_models()    # 建立模型软链
app.start_services() # filebrowser + frpc（守护线程）
app.run_comfyui()    # 前台阻塞；默认由 watchdog 自动重启
# app.run_comfyui(watchdog_enabled=False)  # 关闭看门狗，保持旧行为

# 或者一行：
# app.run()
```

需要“只看命令不执行”时：

```python
cfg = load_config("tts-img.yaml", overrides={"dry_run": True})
app = KaggleComfy(cfg)
app.prepare()
print("\n".join(app.commands))
```

## 配置说明

完整示例见 [`src/kgcomfy/configs/tts-img.yaml`](src/kgcomfy/configs/tts-img.yaml)。
所有字段都有默认值，YAML 只需要写要覆盖的部分。

### `runtime`

| 字段 | 说明 |
| --- | --- |
| `comfy_dir` | ComfyUI 目录，默认 `/kaggle/ComfyUI` |
| `python_dir` | 便携 Python 根目录，默认 `/py312` |
| `python_bin` | Python 可执行文件；留空时自动用 `<python_dir>/bin/python3` |
| `install_bundles` | 是否下载/解压 ComfyUI 与 Python 整包 |
| `apt_packages` | 需要 apt 安装的包，默认 `aria2` |
| `aria2.connections` / `splits` | aria2 并发连接数/分片数 |
| `comfyui_bundle.url` / `source` / `archive` / `extract_to` | ComfyUI 整包下载地址、本地压缩包（Kaggle Dataset，优先于 url）、落盘位置、解压目标 |
| `python_bundle.url` / `source` / `archive` / `extract_to` | 便携 Python 整包，来源选择同上 |
| `comfyui_bundle.overwrite` / `python_bundle.overwrite` | 为 `true` 时 archive 已存在也重新复制/下载，默认 `false` |
| `links` | 需要额外创建的软链，如 `/kaggle/working/ComfyUI` |

### `updates`

| 字段 | 说明 |
| --- | --- |
| `comfyui` | 是否 `git pull` ComfyUI 最新版 |
| `comfyui_requirements` | pull 后是否重装 `requirements.txt` |
| `custom_nodes` | 是否对所有 `enabled` 节点执行 `git pull` |
| `custom_node_requirements` | 更新节点后是否重装其依赖 |

不是 git 仓库的目录（例如整包解压出来的节点）会自动跳过，不会报错。

### `custom_nodes`

```yaml
custom_nodes:
  - name: ComfyUI-VoxCPM
    repo: https://github.com/opq007/ComfyUI-VoxCPM.git
    enabled: true
    install_requirements: true
    ref: ""          # 可选：分支/tag
    reclone: false   # true = 先删目录再 clone
```

- `enabled`：是否由本库管理（缺失时 clone，更新开关打开时 pull）
- `repo` 留空：只更新整包里已有的目录，不负责首次 clone
- `reclone`：用于替换不兼容的旧节点；删除范围被限制在 `custom_nodes/` 内

### `models`

```yaml
models:
  - src: /kaggle/input/.../model.safetensors
    dest: models/clip/model.safetensors   # 相对 comfy_dir
    mode: symlink                         # symlink | copy | hardlink
    force: false                          # 目标已存在且不是软链时是否覆盖
    required: true                        # 源不存在时是否让流程失败
```

也支持简写：

```yaml
models:
  /kaggle/input/.../model.safetensors: models/clip/model.safetensors
```

### `comfyui`

| 字段 | 说明 |
| --- | --- |
| `port` | ComfyUI 监听端口，默认 8188 |
| `args` | 启动参数，`{port}` 会被替换成实际端口 |
| `env` | 启动环境变量，默认 `CUDA_VISIBLE_DEVICES=0,1`、`MPLBACKEND=Agg` |
| `requirements_file` | ComfyUI 依赖文件名 |

### `watchdog`

Phase 1：在启动 ComfyUI 的 Cell 内做进程监管。`app.run_comfyui()` 默认读取
`watchdog.enabled`；需要旧行为时用 `app.run_comfyui(watchdog_enabled=False)`。

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用看门狗，默认 `true` |
| `health_path` | 健康检查路径，默认 `/system_stats`；完整地址为 `http://127.0.0.1:<comfyui.port><health_path>` |
| `interval` / `timeout` | 轮询间隔 / 单次 HTTP 超时（秒） |
| `failures` | 启动宽限期结束后，连续失败多少次触发重启 |
| `startup_grace` | 启动宽限期（秒）；模型加载期间健康检查失败不计数 |
| `backoff_initial` / `backoff_max` | 重启退避初始值/上限（秒），按 2 倍递增 |
| `max_restarts` / `restart_window` | 时间窗口内的重启次数上限；超过后熔断并停止 |
| `shutdown_timeout` | SIGTERM 后等待多久再 SIGKILL（会终止整个进程组） |
| `log_path` | ComfyUI stdout/stderr 日志；异常退出时自动 tail 到 notebook |
| `state_path` | 看门狗状态 JSON，可用 `app.watchdog_status()` 读取 |
| `log_max_bytes` | 日志超过该大小后轮转为 `.1`；运行期间也会轮转，0 表示不轮转 |

行为说明：

- ComfyUI 进程异常退出（退出码非 0）会重启；退出码 0 视为主动停止，看门狗结束。
- 看门狗主动终止假死进程时，即使子进程退出码为 0，也会按异常重启。
- 中断 Cell（Ctrl-C）会优雅停止，不触发重启。
- 进程还活着但 `/system_stats` 连续失败时，先 SIGTERM 整个进程组，再 SIGKILL，然后重启。
- 一旦健康检查成功过，后续失败会立即计数，不受 `startup_grace` 影响。
- 重启后端口不变，frpc/filebrowser 不需要重建；正在排队的任务和已加载模型会丢失，冷启动需要时间。
- 超过 `max_restarts` 后停止并写入 `state=failed`，避免 OOM/坏节点无限重启消耗 GPU 配额。
- 如果模型加载时间超过 `startup_grace`，需要调大该值，否则启动阶段会被判定为失败。

`app.run_comfyui()` / `app.run()` 的返回值：`0` 正常停止，`1` 看门狗熔断或看门狗自身异常，`130` Cell 被中断。

Kaggle 使用建议：

- **交互模式**：启动 ComfyUI 的 Cell 必须保持运行；自动重启只在 Cell 生命周期内有效。
- **无人值守长跑**：用 **Save & Run All**（batch），关掉浏览器也会继续执行，ComfyUI 仍可通过 frp 访问；
  但仍受单次 Session 上限（CPU/GPU 12h、TPU 9h）和每周 GPU 配额限制。
- 看门狗不能跨 Session 存活；Session 停止后 Kaggle 会销毁容器。

### `frp`

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启动 frpc |
| `binary` | frpc 本地文件路径（Kaggle Dataset）或下载 URL；指向目录时自动在其中查找 `frpc`/`frpc.exe` |
| `binary_dest` | 复制/下载后的路径 |
| `config_path` | 生成的 `frpc.toml` 路径 |
| `server_addr` / `server_port` | frp 服务端地址/端口 |
| `token_secret` | 逻辑密钥名，默认 `frp_token` |
| `subdomain` | 子域名，公网地址为 `<subdomain>.<server_addr>` |
| `public_scheme` | 默认 `http` |
| `proxies` | 留空时自动生成 ComfyUI 和 fslist 两个 proxy；filebrowser 关闭时只生成 ComfyUI |

### `filebrowser`

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启动文件浏览器 |
| `binary_url` / `binary_path` | 下载地址与落盘位置 |
| `config_path` | 生成的 filebrowser YAML |
| `base_url` / `port` | 访问前缀与监听端口 |
| `sources` | 暴露给浏览器的目录 |
| `admin_username` | 管理员用户名 |
| `admin_password_secret` | 管理员密码对应的逻辑密钥名 |

### `secrets`

代码只引用逻辑名，真实值来自 Kaggle Secrets 或环境变量：

```yaml
secrets:
  frp_token:
    kaggle: frp_token      # Kaggle Secrets 里的名称
    env: FRP_TOKEN         # 本地/CI 的环境变量名
```

解析顺序：显式 `secret_overrides` → 环境变量 → Kaggle Secrets。
缺失时给出明确提示，例如：

```
missing secret 'frp_token'. Add notebook secret 'frp_token' in Kaggle
(Add-ons -> Secrets), or set environment variable FRP_TOKEN.
```

### `cleanup`

默认关闭。开启后只允许操作 `comfy_dir` 下的路径：

- `paths`：要删除的目录/文件；
- `symlink_globs`：按 glob 删除软链，**不会删除真实文件**。

### `publish`

```yaml
publish:
  modelscope_repo: opqtest007/comfy
  modelscope_token_secret: modelscope_token
  repo_type: dataset
```

对应笔记本末尾的可选 Cell：

```python
pub = app.publisher()
pub.create_tar("/kaggle/working/ComfyUI.tar", "/kaggle/ComfyUI")
pub.modelscope_upload("/kaggle/working/ComfyUI.tar", "ComfyUI.tar")
```

## 迁移对照

| 原笔记本 | 新位置 |
| --- | --- |
| `!apt-get update` / `!apt install aria2` | `runtime.apt_packages` |
| `aria2c` 下载 ComfyUI/Python 整包，或从 Kaggle Dataset 复制本地压缩包 | `runtime.comfyui_bundle` / `runtime.python_bundle` |
| `tar -xf` + `ln -s` | `runtime.links` |
| `git pull` ComfyUI | `updates.comfyui` |
| `./updateNode.sh` / 节点 `git pull` | `updates.custom_nodes` |
| `git clone` 节点 | `custom_nodes[].repo` + `enabled` |
| `rm` 旧模型 / `find -type l -delete` | `cleanup.paths` / `cleanup.symlink_globs` |
| 一长串 `ln -s` 模型 | `models` |
| 单元格里的 `run()` 函数 | `kgcomfy.shell.Runner` |
| frpc 复制、`frpc.toml` 拼接 | `frp` |
| filebrowser 下载、`config.yaml` 拼接 | `filebrowser` |
| `threading.Thread(...).start()` | `app.start_services()` |
| 启动 ComfyUI 的重复命令 | `app.run_comfyui()` |
| 手动重启 ComfyUI / 守护进程 | `watchdog.*` + `app.run_comfyui()` |
| 硬编码 ModelScope token | `secrets.modelscope_token` |
| 从 Kaggle Secrets 取 `frp_token` | `secrets.frp_token` |

## 安全注意事项

- 原笔记本里出现过明文 ModelScope token（`ms-...`）。`legacy/tts-img.original.ipynb`
  中已替换为 `ms-REDACTED-ROTATE-ME`，**建议立即在 ModelScope 后台吊销并重新生成该 token**。
- 新代码不会把密钥写进配置文件或日志；`Runner` 在打印命令时会对 `redact` 参数中的值打码。
- `cleanup` 默认关闭，且所有删除操作都被限制在 `comfy_dir` 下；glob 清理只删软链。
- `reclone` 的删除范围被限制在 `custom_nodes/` 下。

## 本地开发与测试

```bash
# 直接使用源码（无需安装）
python -m unittest discover -s tests -t . -v

# 或者安装为可编辑包
pip install -e ".[dev]"
kgcomfy show-config --config tts-img.yaml
kgcomfy prepare --config tts-img.yaml --dry-run
```

Windows 本地运行时会自动跳过“真实创建软链”的用例（需要开发者模式/管理员权限），
Linux/Kaggle 上会正常执行。其余配置、TOML/YAML 生成、命令拼装、清理安全边界等逻辑均有覆盖。

## 新增一个笔记本

1. 复制 `src/kgcomfy/configs/tts-img.yaml` 为例如 `wan-video.yaml`；
2. 修改整包 URL、模型映射、节点列表、frp 子域名；
3. 新建笔记本，只保留：

```python
cfg = load_config("wan-video.yaml", overrides={"frp": {"subdomain": "wan"}})
app = KaggleComfy(cfg)
app.run()
```

不需要再复制任何安装/软链/frp 代码。
