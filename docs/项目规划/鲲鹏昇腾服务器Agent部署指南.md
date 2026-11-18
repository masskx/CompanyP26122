# 鲲鹏昇腾服务器 Agent 部署指南

> 版本：v2，同步日期 2026-11-17  
> 适用对象：运行在服务器上的 DeepSeek Harness（DSH）Agent。  
> 人工执行手册：[鲲鹏昇腾服务器环境配置手册](./鲲鹏昇腾服务器环境配置手册.md)。  
> 业务代码部署：[代码部署指南](./代码部署指南.md)。

## 0. Agent 执行约定

### 0.1 必须满足的硬约束

1. Agent 的推理模型**只使用 DeepSeek API**。
2. Agent 不得把 DSH 模型切换为本地 vLLM。
3. 本地 vLLM、Embedding 是项目业务服务，不是 Agent 的推理模型。
4. Agent 以 root 运行，但仍必须逐项执行检查，不得跳过失败项。
5. Agent 不保存、不输出 API Key、密码和私钥。
6. Agent 不得执行未替换的 `<...>` 占位符。
7. 每个任务完成后必须写入状态文件；失败时停止当前阶段。
8. 外网 Windows 不连接服务器。外网资源只能经内网中转机上传。

### 0.2 Agent 不可自行决定的事项

以下条件不满足时，Agent 必须停止并请求人工输入：

| 人工门禁 | 必须提供的值 |
|---|---|
| GPU/NPU 型号 | 使用 `npu-smi info -t board -i 0` 的结果确认 |
| 网络参数 | IP、掩码、网关、DNS、VLAN |
| DeepSeek API | 可用的 API Key 和模型名 |
| 模型制品 | 模型仓库、revision、量化格式、许可证 |
| vLLM 镜像 | 与真实卡型匹配的完整 tag |
| 业务部署 | 数据库、证书、主服务器地址等代码部署参数 |

### 0.3 状态文件

Agent 使用 `/root/agent-state.json` 记录进度：

```json
{
  "schema_version": 1,
  "phase": "bootstrap",
  "completed_tasks": [],
  "current_task": null,
  "blockers": [],
  "artifacts": {},
  "last_updated": null
}
```

任务成功后将任务 ID 写入 `completed_tasks`，并记录关键输出。任务失败时写入 `blockers` 并
停止。

### 0.4 执行结果格式

Agent 每次执行任务应输出：

```json
{
  "task_id": "ENV-001",
  "status": "passed",
  "summary": "aarch64, graphical.target, 4TB data disk detected",
  "evidence": ["uname -m", "systemctl get-default", "df -h /data"],
  "artifacts": {},
  "next_task": "NET-001"
}
```

`status` 只允许 `passed`、`failed`、`blocked`。

---

## 阶段 A：人工引导

阶段 A 由人工完成。Agent 在这些任务完成并验证 DSH 可用后启动。

### A-001 配置网络

前置条件：

- 已从同网段参考服务器取得 IP 网段、掩码、网关、DNS 和 VLAN。
- 已确认新服务器 IP 未被占用。

参数文件：

```bash
cat >/root/network.env <<'EOF'
SERVER_IP="<服务器IP>"
SERVER_CIDR="$SERVER_IP/24"
GATEWAY_IP="<默认网关>"
DNS_IP="<DNS>"
EOF
set -a
source /root/network.env
set +a
```

通过条件：

- `env | grep -E '^SERVER_|^GATEWAY_|^DNS_'` 能看到实际值。

命令：

```bash
nmcli con show
nmcli con mod "<连接名>" \
  ipv4.method manual \
  ipv4.addresses "$SERVER_CIDR" \
  ipv4.gateway "$GATEWAY_IP" \
  ipv4.dns "$DNS_IP"
nmcli con up "<连接名>"
ip route
```

通过条件：

- `ip route` 有默认路由。
- 网关可 ping。
- `registry.npmmirror.com`、`www.modelscope.cn` 和 `api.deepseek.com` 的 TLS 连接正常。

失败处理：

- 不继续。
- 将 VLAN、网关或 DNS 问题交由网络管理员处理。

### A-002 配置 SSH

前置条件：

- root 密码已设置。
- 新终端能够使用 root 登录。

配置：

```text
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PubkeyAuthentication yes
UsePAM yes
AllowUsers root
```

验证：

```bash
sshd -T | grep -E \
  'permitrootlogin|passwordauthentication|kbdinteractiveauthentication|allowusers'
```

通过条件：

- 新 SSH 会话可登录。
- 旧 SSH 会话在验证完成前不得关闭。

### A-003 安装 Node.js 与 DSH

参数文件：

```bash
cat >/root/dsh-uv.env <<'EOF'
NODE_VERSION="24.22.3"
DSH_VERSION="0.2.2-rc.1"
UV_VERSION="0.13.8"
UV_PYTHON_DIR="/opt/uv/python"
UV_PYTHON_INSTALL_DIR="/opt/uv/python"
UV_TOOL_DIR="/opt/uv/tools"
UV_TOOL_BIN_DIR="/usr/local/bin"
EOF
set -a
source /root/dsh-uv.env
set +a
```

命令：

```bash
mkdir -p /data/pkg/node
curl -L --fail -o "/data/pkg/node/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
  "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz"

mkdir -p "/opt/node/${NODE_VERSION}"
tar -xJf "/data/pkg/node/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
  -C "/opt/node/${NODE_VERSION}" --strip-components=1
ln -sfn "/opt/node/${NODE_VERSION}" /opt/node/current
ln -sfn /opt/node/current/bin/node /usr/local/bin/node
ln -sfn /opt/node/current/bin/npm /usr/local/bin/npm
ln -sfn /opt/node/current/bin/npx /usr/local/bin/npx

mkdir -p "/opt/dsh/releases/${DSH_VERSION}"
npm install \
  --prefix "/opt/dsh/releases/${DSH_VERSION}" \
  --omit=dev \
  --registry=https://registry.npmmirror.com \
  "@deepseek-ai/dsh@${DSH_VERSION}"
ln -sfn "/opt/dsh/releases/${DSH_VERSION}" /opt/dsh/current
ln -sfn /opt/dsh/current/node_modules/.bin/dsh /usr/local/bin/dsh
```

通过条件：

- `node -v` 为 `v24.22.3`。
- `dsh --version` 为 `0.2.2-rc.1`。

失败处理：

- npmmirror 无法取得 DSH 时，按环境配置手册第 10.10 节 `EXT-DSH` 准备离线包。

### A-004 配置 DSH 使用 DeepSeek API

创建 DSH 目录：

```bash
mkdir -p /root/.dsh /root/dsh-workspace
chmod 700 /root/.dsh
```

启动 DSH：

```bash
cd /root/dsh-workspace
DSH_HOME=/root/.dsh \
  dsh web --host 127.0.0.1 --port 3080 --no-open
```

人工在 DSH Web UI 中完成：

1. 选择 DeepSeek 官方提供商。
2. 保存 DeepSeek API Key。
3. 选择可用的 DeepSeek 模型。
4. 不添加 `local-vllm` provider。
5. 新建会话并确认能正常对话。

通过条件：

- DSH Web UI 可打开。
- DeepSeek 模型可对话。
- Agent 工具执行可用。
- DeepSeek API Key 不出现在对话、日志和工作区文件中。

### A-005 启动 Agent

人工发送首个任务：

```text
读取《鲲鹏昇腾服务器Agent部署指南.md》和 /root/agent-state.json。
从 ENV-001 开始执行。
你只能使用当前 DeepSeek API 模型，禁止切换到本地 vLLM。
任何通过条件失败时停止，不要猜测。
```

通过条件：

- Agent 返回任务 `ENV-001` 的执行结果。
- Agent 没有修改 DSH 模型提供商。

---

## 阶段 B：Agent 执行

### ENV-001 主机前置检查

前置任务：无。

命令：

```bash
uname -m
cat /etc/os-release
uname -r
systemctl get-default
df -h /data
free -g
npu-smi info -t board -i 0
```

通过条件：

- `uname -m` 为 `aarch64`。
- 默认 target 为 `graphical.target`。
- `/data` 空间满足镜像、模型和备份需求。
- 能读取 NPU 型号和 Health。

失败处理：

- 卡型无法识别：`blocked`。
- 磁盘不足：`blocked`。
- 桌面启动级别错误：修正后重试。

### EXT-001 检查外网资源

前置任务：`ENV-001`。

命令：

```bash
test -f /data/pkg/uv/uv-aarch64-unknown-linux-gnu.tar.gz
test -d /data/pkg/driver
test -f /data/pkg/images/vllm-ascend.tar
test -f /data/pkg/images/docker-27-cli.tar
for f in Qwen3-14B.tar Qwen3-30B-A3B.tar bge-m3.tar; do
  test -f "/data/pkg/models/$f" &&
    echo "present: $f" ||
    echo "optional: $f not transferred"
done
cd /data/pkg && sha256sum -c manifest.sha256
```

通过条件：

- uv、驱动、vLLM 镜像和 Controller 基础镜像已存在。
- 模型 tar 可以不存在；Agent 可直接从 ModelScope 下载。
- SHA-256 校验通过。

失败处理：

- 缺少资源时返回 `blocked`。
- 要求人工按环境配置手册第 10 章准备资源。
- Agent 不得尝试从外网 Windows 直接连接服务器。

### SRC-001 配置软件源与 uv

前置任务：`A-001`、`EXT-001`。

命令：

```bash
apt update
apt install -y \
  ca-certificates curl wget git vim nano tar xz-utils unzip \
  net-tools lsof htop tmux chrony build-essential dkms \
  "linux-headers-$(uname -r)" nginx docker.io docker-compose-plugin

curl -LsSf https://astral.sh/uv/install.sh |
  env UV_UNMANAGED_INSTALL=/usr/local/bin sh
uv --version
```

通过条件：

- `apt update` 无 404、签名或架构错误。
- `uv --version` 成功。
- `docker version`、`docker compose version` 成功。

失败处理：

- uv 在线安装失败时，使用第 10.2 节 `EXT-UV`。
- 麒麟源失败时停止，不混用其他发行版源。

### SRC-002 安装 Python 3.12

前置任务：`SRC-001`。

命令：

```bash
mkdir -p /opt/uv/python /opt/uv/tools /usr/local/bin
cat >/etc/profile.d/uv.sh <<'EOF'
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
export UV_TOOL_DIR=/opt/uv/tools
export UV_TOOL_BIN_DIR=/usr/local/bin
EOF
source /etc/profile.d/uv.sh
uv python install 3.12
uv python find 3.12
```

通过条件：

- `uv python find 3.12` 返回 Python 3.12 路径。

失败处理：

- 使用第 10.3 节 `EXT-PY`，不切换回 Conda。

### DRV-001 安装昇腾驱动和固件

前置任务：`ENV-001`、`EXT-001`。

命令：

```bash
cd /data/pkg/driver
chmod +x *.run
./*driver*_linux-aarch64.run --full
./*firmware*_linux-aarch64.run --full
reboot
```

通过条件，重启后检查：

```bash
npu-smi info
ls -l /dev/davinci0 /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc
cat /usr/local/Ascend/driver/version.info
```

- Health 为 OK。
- 四个设备节点存在。

失败处理：

- 重启导致会话中断时，Agent 恢复后从 `DRV-001-check` 继续。
- 驱动或固件缺失、不匹配时停止，并按第 10.4 节 `EXT-DRV` 准备资源。

### DKR-001 配置 Docker

前置任务：`SRC-001`。

命令：

```bash
systemctl enable --now docker
mkdir -p /data/docker
cat >/etc/docker/daemon.json <<'EOF'
{
  "data-root": "/data/docker",
  "exec-opts": ["native.cgroupdriver=systemd"],
  "registry-mirrors": ["<国内Docker镜像站>"],
  "log-driver": "json-file",
  "log-opts": {"max-size": "100m", "max-file": "3"}
}
EOF
systemctl restart docker
docker info
```

通过条件：

- Architecture 为 `aarch64`。
- Docker Root Dir 为 `/data/docker`。

### VLM-001 导入 vLLM 镜像

前置任务：`DRV-001`、`DKR-001`、`EXT-001`。

命令：

```bash
source /etc/mes/vllm.env
docker load -i /data/pkg/images/vllm-ascend.tar
docker image inspect "$VLLM_IMAGE" --format '{{.Architecture}} {{.RepoDigests}}'
```

通过条件：

- `/etc/mes/vllm.env` 中的镜像、模型和 API Key 参数已填写。
- 架构为 `arm64`。
- 镜像 digest 已记录。

### VLM-002 下载或导入模型

前置任务：`VLM-001`。

命令：

```bash
source /root/dsh-uv.env
source /root/model.env
uv tool install modelscope
modelscope download --model "$QWEN14B_REPO" \
  --revision "$QWEN14B_REV" --local_dir "$QWEN14B_DIR"
modelscope download --model "$QWEN30B_REPO" \
  --revision "$QWEN30B_REV" --local_dir "$QWEN30B_DIR"
modelscope download --model "$BGE_M3_REPO" \
  --revision "$BGE_M3_REV" --local_dir "$BGE_M3_DIR"
```

通过条件：

- `QWEN14B_REPO` 和 `QWEN30B_REPO` 已按卡型选择：
  - A2/910B：`Qwen/Qwen3-14B`、`Eco-Tech/Qwen3-30B-A3B-w8a8`
  - 310P：`vllm-ascend/Qwen3-14B-w8a8sc-310-vllm-tp1`、
    `Eco-Tech/Qwen3-30B-A3B-w8a8-QuaRot-310`
- 三个模型目录都有 `config.json` 和权重文件。

失败处理：

- ModelScope 无法访问时返回 `blocked`，要求人工按环境配置手册第 10.7、10.8、10.9 节
  准备 `EXT-M14`、`EXT-M30B`、`EXT-MBGE`。

### VLM-003 启动 vLLM 与 Embedding

前置任务：`VLM-002`。

命令：

```bash
source /etc/mes/vllm.env
/usr/local/bin/start-vllm-chat.sh
/usr/local/bin/start-vllm-embedding.sh
docker logs --tail 100 vllm-chat
docker logs --tail 100 vllm-embedding
```

通过条件：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H "Authorization: Bearer $VLLM_API_KEY" \
  http://127.0.0.1:8000/v1/models
curl -fsS -H "Authorization: Bearer $EMBEDDING_API_KEY" \
  http://127.0.0.1:8002/v1/models
```

- 两个服务都有模型列表。
- vLLM 工具调用测试返回 `tool_calls`。
- A2/910B BF16 的 `CHAT_QUANTIZATION` 为 `none`；310P W8A8 为 `ascend`。

硬约束：

- 本地 vLLM 仅用于项目业务。
- Agent 不得把 DSH 模型切换到本地 vLLM。

### CTL-001 安装 vLLM Controller

前置任务：`VLM-003`、`EXT-001`。

适配检查：

- 附件控制器默认容器名是 `vllm-qwen3-14b`、`vllm-bge-m3`。
- 当前环境若使用其他容器名，必须修改控制器配置。
- 附件 GPU 查询使用 `nvidia-smi`，昇腾环境必须改为 `npu-smi`。

安装：

```bash
mkdir -p /root/vllm/controller
cp /data/pkg/controller/server.py /root/vllm/controller/
cp /data/pkg/controller/Dockerfile /root/vllm/controller/
cp /data/pkg/controller/compose.yaml /root/vllm/controller/
docker load -i /data/pkg/images/docker-27-cli.tar
cd /root/vllm/controller
docker compose build
docker compose up -d
docker compose ps
```

通过条件：

```bash
curl -fsS http://127.0.0.1:8001/healthz
curl -fsS http://127.0.0.1:8001/api/status
```

- `healthz` 返回 `{"ok": true}`。
- 状态接口容器状态与 `docker ps` 一致。

### HND-001 交接代码部署

前置任务：`VLM-003`。

命令：

```bash
test -d /opt/uv/python
test -f /etc/mes/vllm.env
curl -fsS http://127.0.0.1:8000/v1/models
curl -fsS http://127.0.0.1:8002/v1/models
```

通过条件：

- 环境手册要求的模型运行时已完成。
- Agent 不执行代码部署指南中的业务代码步骤，转交代码部署任务。

### FIN-001 DSH 模型隔离检查

前置任务：全部阶段 B。

命令：

```bash
grep -R "local-vllm" /root/.dsh/profiles 2>/dev/null || true
systemctl status dsh-web --no-pager
```

通过条件：

- 未配置 `local-vllm` provider。
- DSH 的模型提供商仍为 DeepSeek。
- Agent 会话仍使用 DeepSeek API。

失败处理：

- 如果 DSH 被切换到本地模型，立即停止并恢复 DeepSeek provider。
- 不删除 DeepSeek API 凭据。

---

## 最终通过条件

Agent 只有在以下项目全部通过后才能报告完成：

```text
ENV-001 passed
EXT-001 passed 或资源不需要
SRC-001 passed
SRC-002 passed
DRV-001 passed
DKR-001 passed
VLM-001 passed
VLM-002 passed
VLM-003 passed
CTL-001 passed 或明确不部署
HND-001 passed
FIN-001 passed
```

最终输出必须包含：

```json
{
  "overall_status": "passed",
  "agent_model_provider": "deepseek",
  "local_vllm_used_by_agent": false,
  "completed_tasks": [],
  "blockers": [],
  "artifacts": {
    "npu_board": null,
    "driver_version": null,
    "vllm_image_digest": null,
    "chat_model_revision": null,
    "embedding_model_revision": null
  }
}
```
