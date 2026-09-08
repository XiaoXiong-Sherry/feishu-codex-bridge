# Feishu Codex Bridge

通过飞书远程使用当前机器或 Workspace 中已经登录的 Codex。Bridge 会创建或恢复 Codex session，在指定目录中查看代码、运行命令和修改文件；它不是在远程操作 Workspace 的图形界面。

本文面向第一次接触飞书开放平台、终端和 Codex 的用户。先完成“快速开始”，需要时再查看命令说明、运行规则和故障排查。

## 1. 功能速览

| 需求 | 使用方式 |
|---|---|
| 让 Codex 执行任务 | 直接给机器人发送普通文字 |
| 查看或切换工作目录 | `/pwd`、`/cd /绝对路径` |
| 开启新对话 | `/new` |
| 恢复历史 session | `/resume`、`/resume --all`、`/select 编号` |
| 重命名 session | `/rename 名称` |
| 选择模型、推理强度和 Fast mode | `/model`、`/reasoning`、`/fast` |
| 查看或停止任务 | `/status`、`/stop` |
| 创建独立会话群 | `/group-create` |
| 永久删除 session | `/delete`、`/delete 编号` |
| 恢复因重启中断的任务 | `/recover-last` |

其他能力：

- 私聊和群聊都可以使用，群聊中不需要 `@机器人`。
- Codex 主动给出的阶段性进度会转发到飞书。
- 不同 Codex session 可以同时执行不同任务。
- 可以进入 Workspace、CLI 或其他本地 Codex 客户端创建的历史 session。
- Bridge 在关闭终端后仍可运行，并在启动或飞书重连时通知使用者。

> [!WARNING]
> Codex 拥有完整的文件和命令权限，不会弹出人工审批窗口。它可以访问当前账号有权访问的用户目录、`/data`、挂载目录和云存储凭证。只允许可信账号使用机器人，执行删除、覆盖或上传任务前要明确写清楚目标。

## 2. 快速开始

配置分为两部分：

1. 飞书开放平台：创建应用，启用机器人，配置权限和事件。
2. Workspace 终端：安装依赖，填写凭证，启动 Bridge。

下文假设仓库位于 `~/feishu-codex-bridge`。如果放在其他位置，请替换项目路径；`start.sh` 会自动识别仓库目录。

### 2.1 准备 Codex 和 Pixi

确认 Codex 已登录：

```bash
codex
```

能够正常进入后退出。Bridge 会复用当前账号已有的 Codex 登录状态和配置。

确认 Pixi 已安装：

```bash
pixi --version
```

如果提示 `command not found`，先按照 [Pixi 官方安装说明](https://pixi.prefix.dev/latest/installation/)安装，再重新打开终端。

### 2.2 配置飞书应用

1. 打开[飞书开放平台](https://open.feishu.cn/app)，进入开发者后台。
2. 创建“企业自建应用”，例如命名为 `Feishu Codex Remote`。
3. 在“添加应用能力”中启用“机器人”。
4. 在“权限管理”中开通以下权限。

| 用途 | 权限代码 |
|---|---|
| 接收用户发给机器人的私聊消息 | `im:message.p2p_msg:readonly` |
| 接收群聊中 `@机器人` 的消息 | `im:message.group_at_msg:readonly` |
| 接收群聊中用户和机器人发送的普通消息 | `im:message.group_msg.include_bot:read` |
| 让机器人以应用身份回复消息 | `im:message:send_as_bot` |
| 让机器人创建会话群 | `im:chat:create` |

5. 在“事件与回调”中选择“使用长连接接收事件”。
6. 添加消息事件 `im.message.receive_v1`。
7. 设置应用可用范围。第一次建议只加入自己。
8. 在“版本管理与发布”中创建版本并发布；如需管理员审核，等待审核通过。
9. 在“凭证与基础信息”中找到 `App ID` 和 `App Secret`。

修改权限、事件或可用范围后，通常需要重新创建版本并发布。不要把 App Secret 提交到 Git 或发送到聊天中。

### 2.3 安装依赖

```bash
cd ~/feishu-codex-bridge
pixi install
```

### 2.4 创建私有凭证文件

```bash
mkdir -p ~/.config/codex-feishu
cp ~/feishu-codex-bridge/.env.example ~/.config/codex-feishu/env
chmod 600 ~/.config/codex-feishu/env
```

打开 `~/.config/codex-feishu/env`，填写：

```bash
FEISHU_APP_ID=你的_App_ID
FEISHU_APP_SECRET=你的_App_Secret
FEISHU_ALLOWED_OPEN_ID=
CODEX_INITIAL_CWD=
CODEX_MODEL=
```

- `FEISHU_ALLOWED_OPEN_ID` 第一次保持为空，下一步获取。
- `CODEX_INITIAL_CWD` 留空时使用当前用户主目录，也可以填写其他绝对路径。
- `CODEX_MODEL` 留空时不强制指定模型。
- 等号两边不要添加空格。

### 2.5 第一次启动并获取 Open ID

Open ID 用于限制谁可以调用 Codex。第一次需要临时在前台启动：

```bash
cd ~/feishu-codex-bridge
umask 077
set -a
source ~/.config/codex-feishu/env
set +a
pixi run python -u bridge.py
```

程序保持运行、终端没有返回提示符是正常现象。

1. 在飞书中私聊机器人并发送“你好”。
2. 机器人会回复你的 Open ID。
3. 复制完整 Open ID，然后回到终端按 `Ctrl+C`。
4. 将 Open ID 写入凭证文件，例如：

```bash
FEISHU_ALLOWED_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxx
```

5. 保存后执行 `chmod 600 ~/.config/codex-feishu/env`。

拿到 Open ID 后应立即填写白名单，不要让机器人长时间处于未限制使用者的状态。

### 2.6 后台启动并验证

```bash
cd ~/feishu-codex-bridge
pixi run start
pixi run status
```

正常情况下：

- 终端显示“机器人正在运行”和 PID。
- 当前启动日志没有错误。
- 飞书私聊收到“Codex Bridge 已上线，飞书连接正常”。

在飞书发送 `/status`，再发送：

```text
请只回复“连接成功”，不要运行命令，不要修改任何文件。
```

收到 `连接成功` 就完成了基本配置。

## 3. 飞书命令

发送 `/help` 可以查看机器人内置命令。

| 命令 | 作用 |
|---|---|
| `/pwd` | 查看当前工作目录 |
| `/cd /绝对路径` | 切换目录，下一条普通任务创建新 session |
| `/new` | 保持目录不变，下一条普通任务创建新 session |
| `/resume [1-20]` | 列出当前目录最近的历史 session，默认 5 个 |
| `/resume --all [1-20]` | 列出全部目录最近的历史 session，默认 5 个 |
| `/select 编号` | 进入历史 session |
| `/rename 新名称` | 重命名当前 session |
| `/model [编号|default]` | 查看、选择或恢复默认模型 |
| `/reasoning [编号|档位|default]` | 查看或选择推理强度 |
| `/fast [on|off]` | 查看或设置 Fast mode |
| `/status` | 查看当前飞书会话和 session 状态 |
| `/stop` | 停止当前任务 |
| `/group-create [临时群名] [绝对路径]` | 创建 Codex 会话群 |
| `/delete [编号]` | 永久删除当前或历史 session |
| `/recover-last` | 检查并继续上次可能中断的任务 |
| `/dismiss-last` | 放弃恢复上次中断的任务 |

### 3.1 目录和新对话

```text
/pwd
/cd /data/project-a
/new
```

- `/cd` 必须使用真实存在的绝对路径。
- `/cd` 和 `/new` 都不会立即创建 session；下一条普通任务才会创建。
- `/cd` 后也可以先用 `/resume` 查找该目录的历史 session。
- 原 session 不会被删除，以后仍可恢复。
- 创建新 session 时，Bridge 会读取当前目录的有效 Codex 配置，并保存当时的模型、Reasoning 和 Fast 作为初始设置。
- 两个命令都会清除当前飞书会话显式设置的模型、Reasoning 和 Fast。
- 两个命令都会清除当前飞书会话保存的旧列表编号和待确认删除。

### 3.2 恢复和重命名 session

```text
/resume
/resume 10
/resume --all
/resume --all 10
```

数量范围是 1～20。列表中的来源表示：

- `[本地 Codex]`：Workspace、VS Code、CLI 或其他本地 Codex 客户端创建。
- `[飞书 Codex]`：当前 Bridge 创建。

进入列表中的 session：

```text
/select 1
```

这会绑定原 Session ID、工作目录和完整上下文，不会创建群聊、运行任务或修改名称。

列表编号是一次 `/resume` 的临时快照：

- `/select 编号` 和 `/delete 编号` 始终使用当前飞书会话最近一次显示的列表，不会暗中重新排序。
- 普通任务或 `/recover-last` 开始处理、`/rename` 成功、session 删除或 Bridge 重启后，旧列表失效。
- `/new` 和 `/cd` 会清除当前飞书会话的旧列表。
- Workspace 或 CLI 中的变化无法提前通知 Bridge；要查看最新排序，请重新发送 `/resume`。

重命名当前 session：

```text
/rename 项目A数据修复
```

名称会写入 Codex 的本地 session 数据。当前没有 session 时，`/rename` 会创建并命名一个空 session；执行第一条普通任务前，它不会出现在 `/resume` 或 `codex resume --all` 中。

### 3.3 模型、Reasoning 和 Fast mode

查看和选择模型：

```text
/model
/model 2
/model default
```

查看和选择当前模型支持的推理档位：

```text
/reasoning
/reasoning 3
/reasoning high
/reasoning default
```

选择模型后，Reasoning 会先恢复为该模型的默认档位。任务执行期间不能切换模型或 Reasoning。

查看和设置 Fast mode：

```text
/fast
/fast on
/fast off
```

- `on`：飞书后续任务使用 Fast 服务档位。
- `off`：飞书后续任务使用普通服务档位。
- 没有 session 时显示“跟随 Codex 本地默认设置”。
- 当前模型不支持 Fast 时不会开启。
- 任务执行期间不能切换 Fast mode。

模型和 Reasoning 的跨客户端规则：

- 飞书明确设置并完成一轮任务后，会成为该 session 后续任务的默认值，但不修改 Workspace 或 CLI 的全局配置。
- 模型和 Reasoning 以该 session 最后一轮任务使用的设置为准，不论任务来自 Workspace、CLI 还是飞书。
- `/select` 进入历史 session 时，会沿用该 session 最后一轮任务的模型和 Reasoning。
- Workspace、CLI 或另一个飞书会话完成新任务后，飞书会在下一条普通任务前提示外部更新，并沿用最新 session 设置。
- 只读 session 接口不返回精确值，因此 `/status` 可能只显示“继承该 Codex session 最后一轮任务设置”。

Fast 是 Bridge 独立保存的请求设置：

- `/fast` 不随 session 跨客户端同步；Workspace、CLI 和飞书各自使用自己的 Fast 设置。
- 同一 session 绑定的多个飞书会话共享 Bridge 保存的 Fast 设置。
- Workspace 或 CLI 更新 session 后，飞书仍保留 Bridge 中原来的 Fast 设置。
- Bridge 只在仍有飞书会话绑定该 session 时保留 Fast 设置；全部解绑后，再次 `/select` 会使用 Codex 本地默认设置。
- `/select` 时，如果该 session 已绑定其他飞书会话，则共享其 Fast 设置；否则跟随当前 Codex 本地默认配置。

### 3.4 状态、进度和停止

`/status` 会显示：

- 当前飞书会话是否空闲或正在执行任务。
- 其他正在执行的飞书会话数量。
- 当前目录、session 名称、来源和完整 ID。
- 当前模型、Reasoning 和 Fast mode 设置。

“空闲”只表示当前飞书会话没有执行任务。Workspace、CLI 或其他客户端是否占用同一 session，会在发送普通任务时检查。

Codex 主动发送阶段性说明时，Bridge 会转发为“Codex 进度”；不会转发内部推理、工具调用细节或终端的每一行原始输出。任务结束后会单独发送最终回答。

停止当前任务：

```text
/stop
```

机器人先回复“已请求停止”，Codex 确认中断后再回复“已确认停止”。`/stop` 不会解除 session 绑定，目录和已有上下文仍会保留。

### 3.5 创建会话群

`/group-create` 会创建一个可以直接与 Codex 对话的飞书群，把发送命令的用户设为群主；新群中不需要 `@机器人`。

当前 session 已经通过 `/rename` 设置正式名称时：

```text
/group-create
```

群名使用 `Codex - 正式名称`。也可以指定只用于本次建群的临时名称：

```text
/group-create 临时处理
```

临时群名不会修改 session 的正式名称。

当前 session 没有正式名称或尚未创建时，必须提供群名：

```text
/group-create 新项目
```

没有 session 时还可以指定目录：

```text
/group-create 数据分析 /data/analysis
```

此时只创建群和初始目录；在新群发送第一条普通任务时才创建 session。

已经绑定 session 时不能通过 `/group-create` 更改目录。如需新目录，先执行：

```text
/new
/cd /data/analysis
/group-create 数据分析
```

同一个 session 可以绑定多个飞书会话，但这些会话不能同时执行任务。

### 3.6 永久删除 session

删除当前飞书会话绑定的 session：

```text
/delete
```

删除历史列表中的其他 session：

```text
/resume
/delete 2
```

机器人会先显示目标名称、目录、完整 ID 和绑定的飞书会话数量。确认无误后，严格按照回复发送：

```text
/delete confirm ID前8位
```

删除规则：

- 确认命令 5 分钟内有效，而且必须是预览后的下一条消息。
- 输入错误确认码或插入其他消息都会取消本次删除。
- 每次只能删除一个 session。
- 正在执行、等待恢复或被其他客户端占用的 session 不能删除。
- 删除不可恢复，但不会删除项目文件、飞书群或聊天记录。
- 所有绑定目标 session 的飞书会话都会解除绑定，目录保持不变；下一条普通任务会创建新 session。

### 3.7 Bridge 重启后的任务恢复

Bridge 启动和飞书重连后会私聊允许使用者：

- 上次任务已结束但结果未送达：补发保存的结果，不重新执行。
- 重启时任务仍在运行：报告“可能中断”，不会自动重复执行。

回到原飞书私聊或群聊处理：

```text
/recover-last
/dismiss-last
```

`/recover-last` 会让 Codex 先检查文件、进程和实际状态，再决定如何继续；`/dismiss-last` 放弃恢复。

## 4. Session 如何工作

### 4.1 创建与绑定

- Bridge 上线通知不会创建 session。
- 没有绑定时，第一条普通任务会创建 session。
- `/new` 和 `/cd` 只等待下一条普通任务，不立即创建。
- `/new`、`/cd` 或新群创建的 session 默认跟随 Codex 本地当前设置。
- `/rename` 可以创建一个有名称的空 session。
- `/group-create` 在没有 session 时只创建飞书群；新群的第一条普通任务才创建 session。
- `/pwd`、`/status`、`/resume`、`/model`、`/reasoning`、`/fast` 和 `/help` 不会创建 session。

每个飞书私聊和群聊分别保存当前目录、Session ID、模型、Reasoning、Fast mode 和最后看到的 turn ID。Bridge 重启后会从本地状态文件恢复。

### 4.2 上下文与外部更新

`/select` 可以进入本地或飞书创建的历史 session，之后的任务会使用完整上下文。

如果 Workspace、CLI 或另一个飞书会话增加了新对话，当前飞书会话在下一次普通任务前会提示：

```text
检测到该 Codex session 在本飞书会话上次操作后有新的对话内容，可能来自 Workspace、CLI 或另一个飞书会话。
本次任务将基于最新上下文继续。
```

这些内容不会复制到飞书聊天记录中，但 Codex 会基于最新上下文继续。

### 4.3 占用与并行

- 不同 session 可以同时执行不同任务。
- 同一个 session 一次只能由一个飞书会话或本地 Codex 客户端执行。
- Workspace 或 CLI 打开并占用 session 时，即使没有运行任务，飞书也可能无法取得执行权。
- `/resume`、`/select`、`/status` 和 `/group-create` 不占用 session。
- 飞书任务完成、失败或停止后会关闭临时 Codex 连接，飞书会话仍绑定原 session。

不同 session 仍共享文件和计算资源。同时修改同一文件或目录可能互相覆盖，建议并行任务使用不同项目目录。

### 4.4 后台任务与进度

Codex 保持当前 turn、持续检查进程并主动汇报时，Bridge 会将进度转发到飞书。

如果 Codex 使用 `nohup`、`tmux` 等方式启动后台进程后结束当前 turn，Bridge 会显示空闲，也无法继续知道后台进程状态。需要持续监视时，请明确要求：

```text
启动任务后保持当前任务运行，定期检查并汇报进度，直到任务结束。
```

Bridge 没有固定任务超时，合法的长任务可以持续运行数小时。

## 5. 运行、安全与数据

### 5.1 启动、停止和重启

```bash
cd ~/feishu-codex-bridge
pixi run start
pixi run status
pixi run stop
```

修改代码或配置后，依次执行 `pixi run stop`、`pixi run start` 和 `pixi run status`。

Bridge 使用 `nohup + setsid` 后台运行，关闭终端不会停止。Workspace、容器或服务器重启后需要手动启动。进程存在不代表飞书长连接一定健康，还应检查飞书上线通知和错误日志。

### 5.2 配置和运行文件

| 路径 | 用途 |
|---|---|
| `~/.config/codex-feishu/env` | App ID、App Secret、Open ID 和默认目录 |
| `~/.config/codex-feishu/state.json` | 飞书会话状态和 session 绑定 |
| `~/.config/codex-feishu/seen.json` | 最近处理的飞书消息 ID |
| `~/.config/codex-feishu/last-task.json` | 最后任务、状态和未送达结果 |
| `~/.config/codex-feishu/feishu-threads.json` | Bridge 创建的 thread ID |
| `~/.local/state/codex-feishu.pid` | Bridge 进程 PID |
| `~/.local/state/codex-feishu.log` | 当前启动日志 |
| `~/.local/state/codex-feishu.log.previous` | 上一次启动日志 |

设置了 `XDG_CONFIG_HOME` 或 `XDG_STATE_HOME` 时，程序会使用对应的标准目录。正常运行时日志可能为空，因为 Bridge 只记录警告和错误。

凭证文件必须只有当前用户可读写：

```bash
chmod 600 ~/.config/codex-feishu/env
stat -c '%a %n' ~/.config/codex-feishu/env
```

### 5.3 安全措施

- `FEISHU_ALLOWED_OPEN_ID` 白名单和飞书应用可用范围共同限制使用者。
- 建立飞书连接后，Bridge 会从 Codex 子进程环境中移除 `FEISHU_APP_SECRET`。
- 最近 2000 条消息 ID 会持久化去重，断线重发不会重复执行。
- 只接受创建时间不超过 30 秒的消息，避免重连后执行积压旧命令。
- 中断任务必须由允许使用者发送 `/recover-last` 才会继续。

这些措施不能消除完整文件权限带来的风险。不要开放给不可信用户，也不要让多个任务同时修改同一批文件。

### 5.4 `/data` 和 S3

只要当前账号有权限，就可以切换到 `/data` 或其他挂载目录：

```text
/cd /data/你的项目目录
```

`s3://bucket/path` 不是本地目录，不能用于 `/cd`。访问 S3 需要 AWS 凭证和工具、已挂载的本地目录，或项目自身提供的 S3 访问方式。Bridge 不会主动移除任务所需的 AWS、S3 等环境变量。

## 6. 常见问题

### 6.1 机器人没有回复

依次确认：

1. 飞书应用已经发布，机器人能力、五项权限和 `im.message.receive_v1` 已配置。
2. 事件订阅使用长连接，自己位于应用可用范围内。
3. `FEISHU_ALLOWED_OPEN_ID` 与自己的 Open ID 完全一致。
4. `pixi run status` 显示 Bridge 正在运行。

修改飞书权限、事件或可用范围后，需要重新创建版本并发布。

### 6.2 Bridge 启动失败

```bash
cd ~/feishu-codex-bridge
pixi run status
tail -50 ~/.local/state/codex-feishu.log
tail -50 ~/.local/state/codex-feishu.log.previous
```

常见原因包括：尚未执行 `pixi install`、凭证文件不存在或权限不是 `600`、App ID/App Secret/Open ID 未填写，以及当前网络无法连接飞书。

### 6.3 SOCKS 或代理错误

项目已经包含 SOCKS 支持。先重新安装依赖并重启：

```bash
cd ~/feishu-codex-bridge
pixi install
pixi run stop
pixi run start
```

如果仍然失败，检查 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY` 是否指向真实可用的代理。不需要代理时，不要为本项目额外设置。

### 6.4 session 被其他客户端占用

等待另一个客户端的任务结束并退出该 session，然后在飞书重新发送原任务。只停止任务但仍保持 session 打开，有时仍会占用。

Bridge 不会抢占或关闭其他客户端，也不会自动重复任务。飞书绑定仍然保留，不需要重新 `/select`。

### 6.5 `/cd` 提示目录不存在

在终端执行 `ls -ld /绝对路径`。如果仍提示不存在，说明路径错误、权限不足或挂载尚未完成。

### 6.6 Workspace 重启后机器人不在线

重新执行：

```bash
cd ~/feishu-codex-bridge
pixi run start
pixi run status
```

如果上次任务可能中断，按照飞书通知使用 `/recover-last` 或 `/dismiss-last`。

## 7. 当前限制

- 飞书断线期间无法立即通知；恢复连接后会发送重连消息并补发已保存的任务结果。
- Bridge 崩溃或 Workspace 重启后不会自动拉起，需要手动启动。
- 日志每次启动时轮转，只保留当前和上一次启动日志。
- 同一个 session 不能同时被 Workspace、CLI 和飞书占用。
- Codex turn 结束后，Bridge 无法继续监视它独立启动的后台进程。
- Codex 的只读 session 接口无法返回最后一轮任务的精确设置；模型和 Reasoning 可以由 session 继续继承，Fast 无法据此跨客户端同步。

## 8. 快速验收

- [ ] 飞书应用已发布，机器人、五项权限和消息事件已配置。
- [ ] 私有凭证文件权限为 `600`，Open ID 已填写。
- [ ] `pixi run status` 显示 Bridge 正在运行且日志没有错误。
- [ ] 飞书发送 `/status` 和 `/pwd` 能收到回复。
- [ ] 普通安全任务能收到 Codex 进度或最终结果。
- [ ] 未授权账号不能调用 Codex。

## 9. 参考资料

- [飞书开放平台](https://open.feishu.cn/app)
- [飞书开放平台相关说明](https://open.feishu.cn/community/articles/7271149634339618818)
- [OpenAI Codex 官方文档](https://developers.openai.com/codex/)
- [Claude Feishu 参考仓库](https://github.com/ZenosZeng/claude-feishu)
