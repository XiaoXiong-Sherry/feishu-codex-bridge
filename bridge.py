import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

cert_file = Path(__file__).resolve().parent / ".pixi/envs/default/ssl/cert.pem"
if cert_file.is_file():
    os.environ.setdefault("SSL_CERT_FILE", str(cert_file))

from lark_channel import ChatQueueConfig, FeishuChannel, LogLevel, PolicyConfig, SafetyConfig
from lark_channel.api.im.v1.model.create_chat_request import CreateChatRequest
from lark_channel.api.im.v1.model.create_chat_request_body import CreateChatRequestBody
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.generated.v2_all import AgentMessageThreadItem, ItemCompletedNotification, MessagePhase, ThreadSortKey, ThreadSourceKind, TurnCompletedNotification
from openai_codex.types import ReasoningEffort


class Bridge:
    def __init__(self, codex, channel):
        self.codex = codex
        self.channel = channel
        self.allowed_open_id = os.getenv("FEISHU_ALLOWED_OPEN_ID", "").strip()
        self.initial_cwd = str(Path(os.getenv("CODEX_INITIAL_CWD") or Path.home()).expanduser().resolve())
        self.model = os.getenv("CODEX_MODEL") or None
        config_dir = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser() / "codex-feishu"
        self.state_path = Path(os.getenv("CODEX_STATE_FILE") or config_dir / "state.json")
        self.seen_path = Path(os.getenv("CODEX_SEEN_FILE") or config_dir / "seen.json")
        self.last_task_path = Path(os.getenv("CODEX_LAST_TASK_FILE") or config_dir / "last-task.json")
        self.feishu_threads_path = Path(os.getenv("CODEX_FEISHU_THREADS_FILE") or config_dir / "feishu-threads.json")
        self.state = self.load_state()
        self.seen_ids = self.load_seen_ids()
        self.last_tasks = self.load_last_tasks()
        registry_exists = self.feishu_threads_path.exists()
        self.feishu_thread_ids = self.load_feishu_thread_ids()
        if not registry_exists:
            self.feishu_thread_ids.update(item["thread_id"] for item in self.state.values() if item.get("thread_id"))
            self.feishu_thread_ids.update(task["thread_id"] for task in self.last_tasks.values() if task.get("thread_id"))
            self.save_feishu_thread_ids()
        self.resume_results = {}
        self.model_results = {}
        self.pending_deletes = {}
        self.active_turns = {}
        self.loop = asyncio.get_running_loop()
        interrupted = False
        for task in self.last_tasks.values():
            if task.get("status") == "running":
                task["status"] = "interrupted"
                task["finished_at"] = int(time.time())
                interrupted = True
        if interrupted:
            self.save_last_tasks()

    def load_state(self):
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text())

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    def load_seen_ids(self):
        if not self.seen_path.exists():
            return []
        return json.loads(self.seen_path.read_text())

    def mark_seen(self, message_id):
        self.seen_ids.append(message_id)
        self.seen_ids = self.seen_ids[-2000:]
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(self.seen_ids, ensure_ascii=False))

    def load_last_tasks(self):
        if not self.last_task_path.exists():
            return {}
        data = json.loads(self.last_task_path.read_text())
        if data.get("chat_id"):
            return {data["chat_id"]: data}
        return data

    def save_last_tasks(self):
        self.last_task_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_task_path.write_text(json.dumps(self.last_tasks, ensure_ascii=False, indent=2))
        self.last_task_path.chmod(0o600)

    def load_feishu_thread_ids(self):
        if not self.feishu_threads_path.exists():
            return set()
        return set(json.loads(self.feishu_threads_path.read_text()))

    def save_feishu_thread_ids(self):
        self.feishu_threads_path.parent.mkdir(parents=True, exist_ok=True)
        self.feishu_threads_path.write_text(json.dumps(sorted(self.feishu_thread_ids), ensure_ascii=False, indent=2))
        self.feishu_threads_path.chmod(0o600)

    async def notify_user(self, text):
        try:
            result = await self.channel.send(self.allowed_open_id, {"text": text})
            if not result.success:
                print(f"飞书通知发送失败：{result.error}", flush=True)
            return result.success
        except Exception as error:
            print(f"飞书通知发送失败：{error}", flush=True)
            return False

    async def deliver_pending_result(self, task, prefix):
        if not task.get("result"):
            return False
        delivered = await self.notify_user(f"{prefix}\n当前目录：{task.get('cwd', self.initial_cwd)}\n\n{task['result']}")
        if delivered:
            task["delivered"] = True
            self.save_last_tasks()
        return delivered

    async def notify_started(self):
        await self.channel.wait_ready()
        pending = [task for task in self.last_tasks.values() if not task.get("delivered") and task.get("status") in {"completed", "failed", "stopped"}]
        for task in pending:
            await self.deliver_pending_result(task, "Codex Bridge 已重新上线。上次任务已经结束，但原回复可能没有送达，现补发结果：")
        interrupted = [task for task in self.last_tasks.values() if task.get("status") == "interrupted"]
        if interrupted:
            summaries = "\n".join(
                f"- {task.get('cwd', self.initial_cwd)}：{task.get('prompt', '').replace(chr(10), ' ')[:120]}"
                for task in interrupted
            )
            await self.notify_user(f"Codex Bridge 已重新上线。\n\n有 {len(interrupted)} 个任务可能因 Bridge 或 Workspace 重启而中断，没有自动重新执行：\n{summaries}\n\n请回到对应的飞书会话发送 /recover-last 检查现场后继续，或发送 /dismiss-last 放弃恢复。")
            return
        if not pending:
            await self.notify_user("Codex Bridge 已上线，飞书连接正常。")

    def on_reconnected(self):
        self.loop.call_soon_threadsafe(self.loop.create_task, self.notify_reconnected())

    async def notify_reconnected(self):
        if self.active_turns:
            await self.notify_user(f"飞书连接已恢复。当前有 {len(self.active_turns)} 个 Codex 任务仍在运行，没有重复执行任务。")
            return
        pending = [task for task in self.last_tasks.values() if not task.get("delivered") and task.get("status") in {"completed", "failed", "stopped"}]
        if pending:
            for task in pending:
                await self.deliver_pending_result(task, "飞书连接已恢复。任务已经结束，但原回复可能没有送达，现补发结果：")
            return

    def session(self, chat_id):
        session = self.state.setdefault(chat_id, {"cwd": self.initial_cwd, "thread_id": None, "last_seen_turn_id": None})
        session.setdefault("model", None)
        session.setdefault("reasoning_effort", None)
        session.setdefault("fast_mode", "auto")
        session.setdefault("inherit_thread_settings", False)
        if session["fast_mode"] == "auto" and session["thread_id"] and session["inherit_thread_settings"]:
            session["fast_mode"] = "inherit"
        return session

    def set_preferences(self, chat_id, model, reasoning_effort):
        session = self.session(chat_id)
        thread_id = session["thread_id"]
        if thread_id:
            for item in self.state.values():
                if item.get("thread_id") == thread_id:
                    item["model"] = model
                    item["reasoning_effort"] = reasoning_effort
                    item["inherit_thread_settings"] = False
        else:
            session["model"] = model
            session["reasoning_effort"] = reasoning_effort
            session["inherit_thread_settings"] = False
        self.save_state()

    def set_fast_mode(self, chat_id, fast_mode):
        session = self.session(chat_id)
        thread_id = session["thread_id"]
        if thread_id:
            for item in self.state.values():
                if item.get("thread_id") == thread_id:
                    item["fast_mode"] = fast_mode
        else:
            session["fast_mode"] = fast_mode
        self.save_state()

    def inherit_thread_settings(self, chat_id):
        session = self.session(chat_id)
        thread_id = session["thread_id"]
        if thread_id:
            for item in self.state.values():
                if item.get("thread_id") == thread_id:
                    item["model"] = None
                    item["reasoning_effort"] = None
                    item["fast_mode"] = "inherit"
                    item["inherit_thread_settings"] = True
        else:
            session["model"] = None
            session["reasoning_effort"] = None
            session["fast_mode"] = "auto"
            session["inherit_thread_settings"] = False
        self.save_state()

    async def available_models(self):
        return (await self.codex.models()).data

    def selected_model(self, session, models):
        if session.get("inherit_thread_settings"):
            return None
        model_name = session.get("model") or self.model
        if model_name:
            return next((item for item in models if item.model == model_name or item.id == model_name), None)
        return next((item for item in models if item.is_default), None)

    def service_tiers(self, model):
        tiers = model.service_tiers or []
        fast = next((tier.id for tier in tiers if tier.id == "fast" or tier.name.lower() == "fast"), None)
        standard = model.default_service_tier
        if not standard or standard == fast:
            standard = next((tier.id for tier in tiers if tier.id != fast), None)
        return fast, standard

    async def effective_service_tier(self, session):
        fast_mode = session.get("fast_mode", "auto")
        if fast_mode == "inherit":
            return None
        if fast_mode == "auto":
            return "default"
        models = await self.available_models()
        current = self.selected_model(session, models)
        if not current:
            return "fast" if fast_mode == "on" else "default"
        fast, standard = self.service_tiers(current)
        if fast_mode == "on":
            if not fast:
                raise RuntimeError("当前模型不支持 Fast mode")
            return fast
        return standard or "default"

    def fast_mode_text(self, session):
        fast_mode = session.get("fast_mode", "auto")
        if fast_mode == "on":
            return "开启（飞书明确设置）"
        if fast_mode == "off":
            return "关闭（飞书明确设置）"
        if fast_mode == "inherit":
            return "沿用该 Codex session 的最新设置（具体开关无法读取）"
        return "关闭（Codex 默认）" if session["thread_id"] else "未设置"

    def active_chat_for_thread(self, thread_id, exclude_chat_id=None):
        if not thread_id:
            return None
        return next(
            (
                chat_id
                for chat_id in self.active_turns
                if chat_id != exclude_chat_id and self.session(chat_id)["thread_id"] == thread_id
            ),
            None,
        )

    async def reply(self, message, text):
        return await self.channel.send(
            message.chat_id,
            {"text": text},
            {"reply_to": message.message_id},
        )

    async def run_turn(self, message, turn):
        completed_turn = None
        final_response = None
        fallback_response = None
        async for event in turn.stream():
            payload = event.payload
            if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn.id:
                item = payload.item.root if hasattr(payload.item, "root") else payload.item
                if isinstance(item, AgentMessageThreadItem):
                    if item.phase == MessagePhase.commentary and item.text.strip():
                        try:
                            await self.reply(message, f"Codex 进度：\n{item.text}")
                        except Exception as error:
                            print(f"Codex 进度发送失败：{error}", flush=True)
                    elif item.phase == MessagePhase.final_answer:
                        final_response = item.text
                    elif item.phase is None:
                        fallback_response = item.text
            elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn.id:
                completed_turn = payload.turn
        if completed_turn is None:
            raise RuntimeError("turn completed event not received")
        if completed_turn.status.value == "failed":
            if completed_turn.error and completed_turn.error.message:
                raise RuntimeError(completed_turn.error.message)
            raise RuntimeError(f"turn failed with status {completed_turn.status.value}")
        return completed_turn.status, final_response or fallback_response

    async def get_thread(self, chat_id, codex):
        session = self.session(chat_id)
        model = None if session.get("inherit_thread_settings") else session.get("model") or self.model
        service_tier = await self.effective_service_tier(session)
        options = {
            "cwd": session["cwd"],
            "sandbox": Sandbox.full_access,
            "approval_mode": ApprovalMode.deny_all,
            "model": model,
            "service_tier": service_tier,
        }
        if session["thread_id"]:
            thread = await codex.thread_resume(session["thread_id"], **options)
        else:
            thread = await codex.thread_start(**options)
            self.feishu_thread_ids.add(thread.id)
            self.save_feishu_thread_ids()
            session["thread_id"] = thread.id
            session["inherit_thread_settings"] = False
            self.save_state()
        return thread

    def execution_error(self, error):
        if "already has an active writer" in str(error):
            return "该 Codex session 已被 Workspace、CLI 或其他客户端占用。\n飞书不能同时执行任务。请先在占用它的客户端中退出该 session，然后重新发送任务。"
        return f"执行失败：{error}"

    async def on_message(self, message):
        if not self.allowed_open_id:
            await self.reply(message, f"你的飞书 Open ID 是：{message.sender_id}\n请把它填入 FEISHU_ALLOWED_OPEN_ID 后重启程序。")
            return
        if message.sender_id != self.allowed_open_id:
            return
        if message.id in self.seen_ids:
            return
        self.mark_seen(message.id)
        text = (message.body_text or message.content_text).strip()
        delete_confirmation = text.split()
        if message.chat_id in self.pending_deletes and not (len(delete_confirmation) == 3 and delete_confirmation[:2] == ["/delete", "confirm"]):
            self.pending_deletes.pop(message.chat_id, None)
        task_prompt = text
        session = self.session(message.chat_id)
        if text == "/status":
            if message.chat_id in self.active_turns:
                status = "正在执行任务"
            elif self.active_chat_for_thread(session["thread_id"], message.chat_id):
                status = "Codex session 正在另一个飞书会话中执行任务"
            else:
                status = "空闲"
            other_count = len(self.active_turns) - int(message.chat_id in self.active_turns)
            thread_lines = ["Codex session：尚未绑定"]
            if session["thread_id"]:
                source = "飞书 Codex" if session["thread_id"] in self.feishu_thread_ids else "本地 Codex"
                thread_lines = ["Codex session：已绑定", "Session 名称：读取失败", f"Session 来源：{source}", f"Session ID：{session['thread_id']}"]
                try:
                    thread_data = (await AsyncThread(self.codex, session["thread_id"]).read()).thread
                    name = thread_data.name or "未命名"
                    thread_lines[1] = f"Session 名称：{name}"
                    if not thread_data.name and thread_data.preview:
                        preview = thread_data.preview.replace("\n", " ")[:80]
                        thread_lines.insert(2, f"Session 摘要：{preview}")
                except Exception:
                    pass
            details = "\n".join(thread_lines)
            if session.get("inherit_thread_settings"):
                model_name = "继承该 Codex session 最后一轮任务设置"
                reasoning_effort = "继承该 Codex session 最后一轮任务设置"
            else:
                model_name = session.get("model") or self.model or "Codex 默认（飞书未强制指定）"
                reasoning_effort = session.get("reasoning_effort") or "模型默认"
            await self.reply(message, f"当前会话状态：{status}\n其他正在执行的会话：{other_count}\n{details}\n当前目录：{session['cwd']}\n当前模型：{model_name}\nReasoning：{reasoning_effort}\nFast mode：{self.fast_mode_text(session)}\n外部客户端占用：发送任务时检查")
            return
        if text == "/stop":
            if message.chat_id not in self.active_turns:
                await self.reply(message, "当前会话没有正在执行的任务。")
                return
            turn = self.active_turns[message.chat_id]
            if turn is None:
                await self.reply(message, "当前任务正在启动，请稍后重新发送 /stop。")
                return
            task = self.last_tasks.get(message.chat_id)
            if task:
                task["stop_requested"] = True
                self.save_last_tasks()
            await turn.interrupt()
            await self.reply(message, "已请求停止当前会话的 Codex 任务。")
            return
        if text == "/delete" or text.startswith("/delete "):
            parts = text.split()
            if len(parts) == 1:
                thread_id = session["thread_id"]
                if not thread_id:
                    await self.reply(message, "当前飞书会话尚未绑定 Codex session，没有可以删除的 session。")
                    return
                cwd = session["cwd"]
            elif len(parts) == 2 and parts[1].isdigit():
                threads = self.resume_results.get(message.chat_id)
                if not threads:
                    await self.reply(message, "请先发送 /resume 获取最新 session 列表。")
                    return
                index = int(parts[1])
                if not 1 <= index <= len(threads):
                    await self.reply(message, f"编号超出当前列表范围：1-{len(threads)}")
                    return
                thread = threads[index - 1]
                thread_id = thread.id
                cwd = thread.cwd.root
            elif len(parts) == 3 and parts[1] == "confirm":
                pending = self.pending_deletes.get(message.chat_id)
                if not pending or time.time() > pending["expires_at"]:
                    self.pending_deletes.pop(message.chat_id, None)
                    await self.reply(message, "删除确认已过期，请重新发送 /delete 或 /delete 编号。")
                    return
                thread_id = pending["thread_id"]
                if parts[2] != thread_id[:8]:
                    self.pending_deletes.pop(message.chat_id, None)
                    await self.reply(message, "删除确认已失效，请重新发送 /delete 或 /delete 编号。")
                    return
                if message.chat_id in self.active_turns or self.active_chat_for_thread(thread_id):
                    await self.reply(message, "该 Codex session 正在执行任务，任务完成或停止后才能删除。")
                    return
                if any(task.get("thread_id") == thread_id and task.get("status") in {"running", "interrupted"} for task in self.last_tasks.values()):
                    await self.reply(message, "该 Codex session 有等待处理的中断任务。请先在对应飞书会话使用 /recover-last 或 /dismiss-last。")
                    return
                process = await asyncio.create_subprocess_exec(
                    "codex",
                    "delete",
                    "--force",
                    thread_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                if process.returncode:
                    error = (stderr or stdout).decode(errors="replace").strip() or f"退出码 {process.returncode}"
                    await self.reply(message, self.execution_error(error).replace("执行失败：", "永久删除 Codex session 失败：", 1))
                    return
                current_deleted = session["thread_id"] == thread_id
                bound_count = 0
                for item in self.state.values():
                    if item.get("thread_id") == thread_id:
                        item["thread_id"] = None
                        item["last_seen_turn_id"] = None
                        item["fast_mode"] = "auto"
                        if item.get("inherit_thread_settings"):
                            item["inherit_thread_settings"] = False
                        bound_count += 1
                self.save_state()
                self.feishu_thread_ids.discard(thread_id)
                self.save_feishu_thread_ids()
                self.resume_results.clear()
                self.pending_deletes = {chat_id: pending for chat_id, pending in self.pending_deletes.items() if pending["thread_id"] != thread_id}
                if current_deleted:
                    result = f"当前飞书会话已解除绑定。\n当前目录保持为：{session['cwd']}\n下一条普通消息会创建新的 Codex session。"
                elif not session["thread_id"]:
                    result = f"当前飞书会话仍未绑定 Codex session。\n当前目录保持为：{session['cwd']}"
                else:
                    result = "当前飞书会话原来绑定的 Codex session 保持不变。"
                await self.reply(message, f"已永久删除 Codex session：{thread_id}\n已解除 {bound_count} 个飞书会话的绑定。\n{result}")
                return
            else:
                await self.reply(message, "格式：/delete 或 /delete 编号")
                return
            if message.chat_id in self.active_turns or self.active_chat_for_thread(thread_id):
                await self.reply(message, "该 Codex session 正在执行任务，任务完成或停止后才能删除。")
                return
            if any(task.get("thread_id") == thread_id and task.get("status") in {"running", "interrupted"} for task in self.last_tasks.values()):
                await self.reply(message, "该 Codex session 有等待处理的中断任务。请先在对应飞书会话使用 /recover-last 或 /dismiss-last。")
                return
            try:
                thread_data = (await AsyncThread(self.codex, thread_id).read()).thread
            except Exception as error:
                await self.reply(message, f"读取待删除 Codex session 失败：{error}")
                return
            name = (thread_data.name or thread_data.preview or "未命名").replace("\n", " ")[:80]
            bound_count = sum(item.get("thread_id") == thread_id for item in self.state.values())
            token = thread_id[:8]
            if session["thread_id"] == thread_id:
                relation = "当前飞书对话绑定的 session"
            elif session["thread_id"]:
                relation = "其他历史 session，当前飞书对话的原绑定不受影响"
            else:
                relation = "其他历史 session，当前飞书对话目前未绑定 session"
            self.pending_deletes[message.chat_id] = {"thread_id": thread_id, "expires_at": time.time() + 300}
            await self.reply(message, f"即将永久删除 Codex session：\n\n目标关系：{relation}\n名称：{name}\n目录：{cwd}\nSession ID：{thread_id}\n当前绑定的飞书会话：{bound_count} 个\n\n删除后无法恢复，不会删除项目文件，也不会解散飞书群。确认命令 5 分钟内有效。\n确认删除请输入：\n/delete confirm {token}")
            return
        if text == "/recover-last":
            last_task = self.last_tasks.get(message.chat_id)
            if not last_task or last_task.get("status") != "interrupted":
                await self.reply(message, "没有需要恢复的中断任务。")
                return
            task_prompt = last_task["prompt"]
            text = f"上一个任务可能只完成了一部分。请先检查当前文件、进程和实际状态，不要重复已经完成的操作，然后继续完成原任务：\n\n{task_prompt}"
        if text == "/dismiss-last":
            last_task = self.last_tasks.get(message.chat_id)
            if not last_task or last_task.get("status") != "interrupted":
                await self.reply(message, "没有需要放弃的中断任务。")
                return
            last_task["status"] = "dismissed"
            last_task["finished_at"] = int(time.time())
            self.save_last_tasks()
            await self.reply(message, "已放弃恢复上次中断的任务。")
            return
        if text == "/pwd":
            await self.reply(message, session["cwd"])
            return
        if text == "/help":
            await self.reply(message, "/pwd 查看当前目录\n/cd /绝对路径 切换目录\n/resume [1-20] 列出当前目录最近的 Codex session\n/resume --all [1-20] 列出全部目录最近的 Codex session\n/select 编号 进入指定 Codex session\n/new 开启新对话\n/rename 新名称 重命名当前 Codex session\n/model [编号|default] 查看或选择模型\n/reasoning [编号|档位|default] 查看或选择推理强度\n/fast [on|off] 查看或设置 Fast mode\n/delete [编号] 永久删除当前或列表中的 Codex session\n/group-create [临时群名] [绝对路径] 创建会话群\n/status 查看当前会话状态\n/stop 停止当前会话任务\n/recover-last 恢复上次中断任务\n/dismiss-last 放弃恢复上次任务")
            return
        if text == "/model" or text.startswith("/model "):
            parts = text.split()
            if len(parts) > 2:
                await self.reply(message, "格式：/model [编号|default]")
                return
            try:
                models = await self.available_models()
            except Exception as error:
                await self.reply(message, f"读取可用模型失败：{error}")
                return
            current = self.selected_model(session, models)
            if len(parts) == 1:
                self.model_results[message.chat_id] = models
                if session.get("inherit_thread_settings"):
                    current_name = "继承该 Codex session 最后一轮任务设置"
                    current_effort = "继承该 Codex session 最后一轮任务设置"
                else:
                    current_name = current.display_name if current else session.get("model") or self.model or "Codex 默认"
                    default_effort = current.default_reasoning_effort.value if current else "未知"
                    current_effort = session.get("reasoning_effort") or f"{default_effort}（模型默认）"
                rows = [f"{index}. {item.display_name}" for index, item in enumerate(models, 1)]
                await self.reply(message, f"当前模型：{current_name}\n当前 Reasoning：{current_effort}\n\n可用模型：\n" + "\n".join(rows) + "\n\n选择模型：/model 编号\n恢复默认：/model default")
                return
            if message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能切换模型。")
                return
            if self.active_chat_for_thread(session["thread_id"], message.chat_id):
                await self.reply(message, "该 Codex session 正在另一个飞书会话中执行任务，任务完成后才能切换模型。")
                return
            value = parts[1]
            if value == "default":
                self.set_preferences(message.chat_id, None, None)
                fallback = self.model or "Codex 默认"
                await self.reply(message, f"已恢复默认模型：{fallback}\nReasoning：模型默认")
                return
            if value.isdigit():
                cached = self.model_results.get(message.chat_id)
                if not cached:
                    await self.reply(message, "请先发送 /model 获取最新模型列表。")
                    return
                index = int(value)
                if not 1 <= index <= len(cached):
                    await self.reply(message, f"编号超出当前列表范围：1-{len(cached)}")
                    return
                selected = cached[index - 1]
                if not any(item.model == selected.model for item in models):
                    await self.reply(message, "模型列表已经变化，请重新发送 /model。")
                    return
            else:
                await self.reply(message, "格式：/model [编号|default]")
                return
            self.set_preferences(message.chat_id, selected.model, None)
            efforts = "、".join(option.reasoning_effort.value for option in selected.supported_reasoning_efforts)
            await self.reply(message, f"已选择模型：{selected.display_name}\nReasoning：{selected.default_reasoning_effort.value}（模型默认）\n可选档位：{efforts}\n使用 /reasoning 选择推理强度。")
            return
        if text == "/reasoning" or text.startswith("/reasoning "):
            parts = text.split()
            if len(parts) > 2:
                await self.reply(message, "格式：/reasoning [编号|档位|default]")
                return
            if len(parts) == 2 and message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能切换 Reasoning。")
                return
            if len(parts) == 2 and self.active_chat_for_thread(session["thread_id"], message.chat_id):
                await self.reply(message, "该 Codex session 正在另一个飞书会话中执行任务，任务完成后才能切换 Reasoning。")
                return
            try:
                models = await self.available_models()
            except Exception as error:
                await self.reply(message, f"读取可用模型失败：{error}")
                return
            if session.get("inherit_thread_settings"):
                await self.reply(message, "当前 Codex session 将继承最后一轮任务的模型和 Reasoning，可能来自 Workspace、CLI 或飞书。公开只读接口不会返回它的精确模型；如需修改 Reasoning，请先使用 /model 选择模型。")
                return
            current = self.selected_model(session, models)
            if not current:
                await self.reply(message, "当前模型不在可用模型列表中，请先使用 /model 选择模型。")
                return
            efforts = [option.reasoning_effort.value for option in current.supported_reasoning_efforts]
            if len(parts) == 1:
                current_effort = session.get("reasoning_effort") or f"{current.default_reasoning_effort.value}（模型默认）"
                rows = [f"{index}. {effort}" for index, effort in enumerate(efforts, 1)]
                await self.reply(message, f"当前模型：{current.display_name}\n当前 Reasoning：{current_effort}\n\n可选档位：\n" + "\n".join(rows) + "\n\n选择档位：/reasoning 编号 或 /reasoning 档位\n恢复模型默认：/reasoning default")
                return
            value = parts[1]
            if value == "default":
                selected_effort = None
            elif value.isdigit():
                index = int(value)
                if not 1 <= index <= len(efforts):
                    await self.reply(message, f"编号超出当前列表范围：1-{len(efforts)}")
                    return
                selected_effort = efforts[index - 1]
            elif value in efforts:
                selected_effort = value
            else:
                await self.reply(message, f"当前模型不支持 Reasoning 档位：{value}\n可选档位：{'、'.join(efforts)}")
                return
            self.set_preferences(message.chat_id, session.get("model"), selected_effort)
            shown_effort = selected_effort or f"{current.default_reasoning_effort.value}（模型默认）"
            await self.reply(message, f"已设置 Reasoning：{shown_effort}\n当前模型：{current.display_name}")
            return
        if text == "/fast" or text.startswith("/fast "):
            parts = text.split()
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"on", "off"}):
                await self.reply(message, "格式：/fast [on|off]")
                return
            if len(parts) == 1:
                support = "当前模型由 Codex session 继承，精确支持状态无法读取"
                try:
                    models = await self.available_models()
                    current = self.selected_model(session, models)
                    if current:
                        fast, _ = self.service_tiers(current)
                        support = f"当前模型支持 Fast mode：{'是' if fast else '否'}"
                except Exception as error:
                    support = f"读取模型支持状态失败：{error}"
                await self.reply(message, f"Fast mode：{self.fast_mode_text(session)}\n{support}\n\n开启：/fast on\n关闭：/fast off")
                return
            if message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能切换 Fast mode。")
                return
            if self.active_chat_for_thread(session["thread_id"], message.chat_id):
                await self.reply(message, "该 Codex session 正在另一个飞书会话中执行任务，任务完成后才能切换 Fast mode。")
                return
            fast_mode = parts[1]
            if fast_mode == "on":
                try:
                    models = await self.available_models()
                    current = self.selected_model(session, models)
                except Exception as error:
                    await self.reply(message, f"读取可用模型失败：{error}")
                    return
                if current and not self.service_tiers(current)[0]:
                    await self.reply(message, "当前模型不支持 Fast mode，设置未修改。\n请使用 /model 查看或切换模型。")
                    return
            self.set_fast_mode(message.chat_id, fast_mode)
            if fast_mode == "on":
                response = "已开启 Fast mode。\n飞书下一条任务将使用 Fast 服务档位。"
            else:
                response = "已关闭 Fast mode。\n飞书下一条任务将使用普通服务档位。"
            await self.reply(message, response)
            return
        if text == "/rename" or text.startswith("/rename "):
            name = text[len("/rename"):].strip()
            if not name:
                await self.reply(message, "格式：/rename 新名称")
                return
            if message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能重命名 Codex session。")
                return
            if self.active_chat_for_thread(session["thread_id"], message.chat_id):
                await self.reply(message, "该 Codex session 正在另一个飞书会话中执行任务，请等待完成后再重命名。")
                return
            created = not session["thread_id"]
            try:
                async with AsyncCodex() as codex:
                    thread = await self.get_thread(message.chat_id, codex)
                    await thread.set_name(name)
            except Exception as error:
                response = self.execution_error(error)
                if not response.startswith("该 Codex session"):
                    response = f"重命名 Codex session 失败：{error}"
                await self.reply(message, response)
                return
            self.resume_results.clear()
            action = "已创建并命名当前 Codex session" if created else "已将当前 Codex session 重命名"
            await self.reply(message, f"{action}：{name}")
            return
        if text == "/resume" or text.startswith("/resume "):
            self.resume_results.pop(message.chat_id, None)
            parts = text.split()
            all_directories = False
            if len(parts) == 1:
                limit = 5
            elif len(parts) == 2 and parts[1] == "--all":
                all_directories = True
                limit = 20
            elif len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= 20:
                limit = int(parts[1])
            elif len(parts) == 3 and parts[1] == "--all" and parts[2].isdigit() and 1 <= int(parts[2]) <= 20:
                all_directories = True
                limit = int(parts[2])
            else:
                await self.reply(message, "格式：/resume [1-20] 或 /resume --all [1-20]")
                return
            try:
                options = dict(
                    limit=max(limit, 20),
                    sort_key=ThreadSortKey.updated_at,
                    source_kinds=[ThreadSourceKind.cli, ThreadSourceKind.vscode, ThreadSourceKind.app_server],
                )
                if not all_directories:
                    options["cwd"] = session["cwd"]
                threads = []
                thread_ids = set()
                cursor = None
                while len(threads) < limit:
                    if cursor:
                        options["cursor"] = cursor
                    result = await self.codex.thread_list(**options)
                    for thread in result.data:
                        if thread.id not in thread_ids:
                            thread_ids.add(thread.id)
                            threads.append(thread)
                    cursor = result.next_cursor
                    if not cursor:
                        break
                threads.sort(key=lambda thread: thread.updated_at, reverse=True)
                threads = threads[:limit]
            except Exception as error:
                await self.reply(message, f"读取 Codex session 失败：{error}")
                return
            if not threads:
                scope = "全部目录" if all_directories else f"当前目录：{session['cwd']}"
                await self.reply(message, f"{scope}没有找到 Codex session。")
                return
            rows = []
            for index, thread in enumerate(threads, 1):
                source_name = "飞书 Codex" if thread.id in self.feishu_thread_ids else "本地 Codex"
                updated_at = datetime.fromtimestamp(thread.updated_at).strftime("%m-%d %H:%M")
                title = (thread.name or thread.preview or "无标题").replace("\n", " ")[:50]
                cwd = f"｜{thread.cwd.root}" if all_directories else ""
                rows.append(f"{index}. [{source_name}] {updated_at}｜{title}{cwd}｜{thread.id[:8]}")
            self.resume_results[message.chat_id] = threads
            heading = f"全部目录最近更新的 {len(rows)} 个 Codex session：" if all_directories else f"当前目录：{session['cwd']}\n最近更新的 {len(rows)} 个 Codex session："
            await self.reply(message, heading + "\n\n" + "\n".join(rows) + "\n\n进入指定 session：/select 编号，例如 /select 1\n永久删除列表中的 session：/delete 编号，例如 /delete 2（仍需二次确认）")
            return
        if text == "/select" or text.startswith("/select "):
            if message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能切换 Codex session。")
                return
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                await self.reply(message, "格式：/select 编号")
                return
            threads = self.resume_results.get(message.chat_id)
            if not threads:
                await self.reply(message, "请先发送 /resume 获取最新 session 列表。")
                return
            index = int(parts[1])
            if not 1 <= index <= len(threads):
                await self.reply(message, f"编号超出当前列表范围：1-{len(threads)}")
                return
            thread = threads[index - 1]
            try:
                async with AsyncCodex() as codex:
                    thread_data = (await AsyncThread(codex, thread.id).read(include_turns=True)).thread
            except Exception as error:
                await self.reply(message, f"读取 Codex session 失败：{error}")
                return
            session["cwd"] = thread.cwd.root
            session["thread_id"] = thread.id
            session["last_seen_turn_id"] = thread_data.turns[-1].id if thread_data.turns else None
            self.inherit_thread_settings(message.chat_id)
            title = thread.name or thread.preview or thread.id[:8]
            notice = ""
            if self.active_chat_for_thread(thread.id, message.chat_id):
                notice = "\n该 session 正在另一个飞书会话中执行任务，任务完成后才能继续发送任务。"
            await self.reply(message, f"已进入 Codex session：{title}\n当前目录：{thread.cwd.root}\n直接发送消息即可继续对话。{notice}")
            return
        if text == "/group-create" or text.startswith("/group-create "):
            if message.chat_id in self.active_turns:
                await self.reply(message, "当前会话正在执行任务，任务完成后才能创建会话群。")
                return
            arguments = text[len("/group-create"):].strip()
            temporary_name = ""
            cwd_text = ""
            if arguments.startswith("/"):
                cwd_text = arguments
            elif " /" in arguments:
                temporary_name, cwd_text = arguments.split(" /", 1)
                temporary_name = temporary_name.strip()
                cwd_text = f"/{cwd_text}"
            else:
                temporary_name = arguments
            cwd = Path(cwd_text or session["cwd"]).expanduser().resolve()
            if not cwd.is_dir():
                await self.reply(message, f"目录不存在：{cwd}")
                return
            if session["thread_id"] and str(cwd) != str(Path(session["cwd"]).resolve()):
                await self.reply(message, "当前已进入 Codex session，不能为它指定其他目录。请先使用 /new 和 /cd 切换到新目录。")
                return
            formal_name = ""
            last_seen_turn_id = None
            if session["thread_id"]:
                try:
                    async with AsyncCodex() as codex:
                        thread_data = (await AsyncThread(codex, session["thread_id"]).read(include_turns=True)).thread
                    formal_name = thread_data.name or ""
                    last_seen_turn_id = thread_data.turns[-1].id if thread_data.turns else None
                except Exception as error:
                    await self.reply(message, f"读取当前 Codex session 失败：{error}")
                    return
            group_name = temporary_name or formal_name
            if not group_name:
                if session["thread_id"]:
                    await self.reply(message, "当前 session 没有正式名称，请提供临时群名：\n/group-create 临时群名")
                else:
                    example = f"/group-create 临时群名 {cwd}" if cwd_text else "/group-create 临时群名"
                    await self.reply(message, f"当前尚未进入 Codex session，请提供临时群名：\n{example}")
                return
            body = CreateChatRequestBody.builder().name(f"Codex - {group_name}").owner_id(self.allowed_open_id).build()
            request = CreateChatRequest.builder().user_id_type("open_id").request_body(body).build()
            response = await self.channel.client.im.v1.chat.acreate(request)
            if not response.success():
                await self.reply(message, f"创建群失败：{response.msg}（错误码：{response.code}）")
                return
            chat_id = response.data.chat_id
            self.state[chat_id] = {
                "cwd": str(cwd),
                "thread_id": session["thread_id"],
                "last_seen_turn_id": last_seen_turn_id,
                "model": session.get("model"),
                "reasoning_effort": session.get("reasoning_effort"),
                "fast_mode": session.get("fast_mode", "auto"),
                "inherit_thread_settings": session.get("inherit_thread_settings"),
            }
            self.save_state()
            if session["thread_id"]:
                group_message = f"已绑定现有 Codex session。\n当前目录：{cwd}\n直接发送消息即可，不需要 @机器人。"
            else:
                group_message = f"会话群已创建，尚未创建 Codex session。\n当前目录：{cwd}\n发送第一条普通任务时会自动创建 session。"
            await self.channel.send(chat_id, {"text": group_message})
            await self.reply(message, f"已创建群：Codex - {group_name}\n当前目录：{cwd}")
            return
        if message.chat_id in self.active_turns:
            await self.reply(message, "当前会话已有 Codex 任务正在执行，请完成后重新发送。")
            return
        if text.startswith("/cd "):
            cwd = Path(text[4:].strip()).expanduser().resolve()
            if not cwd.is_dir():
                await self.reply(message, f"目录不存在：{cwd}")
                return
            session["cwd"] = str(cwd)
            session["thread_id"] = None
            session["last_seen_turn_id"] = None
            session["fast_mode"] = "auto"
            if session.get("inherit_thread_settings"):
                session["model"] = None
                session["reasoning_effort"] = None
            session["inherit_thread_settings"] = False
            self.resume_results.pop(message.chat_id, None)
            self.pending_deletes.pop(message.chat_id, None)
            self.save_state()
            await self.reply(message, f"已切换到：{cwd}\n尚未创建新 Codex session。发送 /resume 可查看历史 session，发送普通任务会开启新对话。")
            return
        if text == "/new":
            session["thread_id"] = None
            session["last_seen_turn_id"] = None
            session["fast_mode"] = "auto"
            if session.get("inherit_thread_settings"):
                session["model"] = None
                session["reasoning_effort"] = None
            session["inherit_thread_settings"] = False
            self.resume_results.pop(message.chat_id, None)
            self.pending_deletes.pop(message.chat_id, None)
            self.save_state()
            await self.reply(message, f"已开启新对话。\n当前目录：{session['cwd']}")
            return
        if self.active_chat_for_thread(session["thread_id"], message.chat_id):
            await self.reply(message, "该 Codex session 正在另一个飞书会话中执行任务，请等待任务完成后再试。")
            return
        self.active_turns[message.chat_id] = None
        try:
            async with AsyncCodex() as codex:
                external_update = False
                if session["thread_id"]:
                    thread_data = (await AsyncThread(codex, session["thread_id"]).read(include_turns=True)).thread
                    latest_turn_id = thread_data.turns[-1].id if thread_data.turns else None
                    last_seen_turn_id = session.get("last_seen_turn_id")
                    external_update = last_seen_turn_id is not None and latest_turn_id != last_seen_turn_id
                    if external_update:
                        self.inherit_thread_settings(message.chat_id)
                thread = await self.get_thread(message.chat_id, codex)
                self.resume_results.clear()
                service_tier = await self.effective_service_tier(session)
                notice = "检测到该 Codex session 在本飞书会话上次操作后有新的对话内容，可能来自 Workspace、CLI 或另一个飞书会话。\n本次任务将基于最新上下文继续。\n\n" if external_update else ""
                await self.reply(message, f"{notice}Codex 正在处理……\n当前目录：{session['cwd']}")
                task = {
                    "status": "running",
                    "chat_id": message.chat_id,
                    "chat_type": message.chat_type,
                    "thread_id": thread.id,
                    "cwd": session["cwd"],
                    "prompt": task_prompt,
                    "model": None if session.get("inherit_thread_settings") else session.get("model") or self.model,
                    "reasoning_effort": None if session.get("inherit_thread_settings") else session.get("reasoning_effort"),
                    "service_tier": service_tier,
                    "started_at": int(time.time()),
                    "delivered": False,
                }
                self.last_tasks[message.chat_id] = task
                self.save_last_tasks()
                model = None if session.get("inherit_thread_settings") else session.get("model") or self.model
                effort = ReasoningEffort(session["reasoning_effort"]) if not session.get("inherit_thread_settings") and session.get("reasoning_effort") else None
                turn = await thread.turn(text, model=model, effort=effort, service_tier=service_tier)
                self.active_turns[message.chat_id] = turn
                session["last_seen_turn_id"] = turn.id
                self.save_state()
                result_status, final_response = await self.run_turn(message, turn)
            if result_status.value == "interrupted":
                response = "已确认停止当前会话的 Codex 任务。当前 session 和上下文仍会保留。" if task.get("stop_requested") else "Codex 任务已中断。当前 session 和上下文仍会保留。"
                task["status"] = "stopped"
                task["result"] = response
                task["finished_at"] = int(time.time())
                self.save_last_tasks()
                delivery = await self.reply(message, response)
                if delivery.success:
                    task["delivered"] = True
                    self.save_last_tasks()
            else:
                response = final_response or f"Codex 未返回文本结果，状态：{result_status}"
                task["status"] = "completed" if result_status.value == "completed" else "failed"
                task["result"] = response
                task["finished_at"] = int(time.time())
                self.save_last_tasks()
                delivery = await self.reply(message, response)
                if delivery.success:
                    task["delivered"] = True
                    self.save_last_tasks()
        except Exception as error:
            response = self.execution_error(error)
            task = self.last_tasks.get(message.chat_id)
            if task and task.get("status") == "running":
                task["status"] = "failed"
                task["result"] = response
                task["finished_at"] = int(time.time())
                self.save_last_tasks()
            delivery = await self.reply(message, response)
            if task and task.get("status") == "failed" and delivery.success:
                task["delivered"] = True
                self.save_last_tasks()
        finally:
            self.active_turns.pop(message.chat_id, None)


async def main():
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    allowed_open_id = os.getenv("FEISHU_ALLOWED_OPEN_ID", "").strip()
    policy = PolicyConfig(
        dm_policy="allowlist" if allowed_open_id else "open",
        group_policy="open",
        require_mention=False,
        allow_from=[allowed_open_id] if allowed_open_id else None,
    )
    channel = FeishuChannel(app_id=app_id, app_secret=app_secret, policy=policy, safety=SafetyConfig(chat_queue=ChatQueueConfig(enabled=False), stale_message_window_ms=30_000), log_level=LogLevel.WARNING)
    os.environ.pop("FEISHU_APP_SECRET", None)
    async with AsyncCodex() as codex:
        bridge = Bridge(codex, channel)
        channel.on("message", bridge.on_message)
        channel.on("reconnected", bridge.on_reconnected)
        await channel.start_background()
        await bridge.notify_started()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
