"""部署 Agent 业务工具集。

对应需求文档第四章 Tools 设计与第六章工具定义：
5 个工具全部通过 SSH（paramiko）在目标服务器上执行，不在本机直接跑 git/docker。

- @tool 装饰 + async 实现
- 参数校验失败 / SSH 异常 / 命令失败统一转结构化 JSON 字符串返回，绝不 raise
- build_xxx_tool(settings) 闭包返回 @tool，build_tools(settings) 汇总导出

日志约定（全局 logger）：
- 进入：logger.info("tool={} | args={}", name, 摘要(密码/token 打码))
- 成功：logger.info("tool={} | ok | elapsed={}ms", name, 耗时)
- 失败：logger.error("tool={} | error={} | elapsed={}ms", name, err, 耗时, exc_info=True)
- SSH 工具额外：logger.info("tool={} | host={} | cmd={}", name, host, 命令摘要)
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import paramiko
from langchain.tools import tool
from loguru import logger

from deploy_agent.settings import Settings

# ==================== 辅助函数 ====================


def _mask(secret: str | None) -> str:
    """敏感信息打码：只露首尾 2 字符。None/空返回 <empty>。"""
    if not secret:
        return "<empty>"
    if len(secret) <= 4:
        return "***"
    return f"{secret[:2]}***{secret[-2:]}"


def _ok(**fields: Any) -> str:
    """构造结构化成功 JSON 字符串。"""
    return json.dumps({"success": True, **fields}, ensure_ascii=False)


def _err(error_type: str, message: str, **extra: Any) -> str:
    """构造结构化错误 JSON 字符串。"""
    return json.dumps(
        {"success": False, "error_type": error_type, "message": message, **extra},
        ensure_ascii=False,
    )


def _elapsed_ms(start: float) -> int:
    """计算耗时（毫秒）。"""
    return int((time.perf_counter() - start) * 1000)


def _build_repo_url_with_auth(repo_url: str, settings: Settings) -> str:
    """把 gitlab_user:gitlab_token 注入用户传入的 repo_url 用于鉴权克隆。

    repo_url 由用户在对话中指定（无白名单），鉴权信息仍从 settings 读取。
    日志中不输出此值（含鉴权信息）。

    安全要点：user/token 必须先 URL 编码再拼进 URL。
    否则 token 含 `@`/`:`/`/`（如 LLy@xxx）或 user 含前导空格时，
    会破坏 `user:token@host` 结构，git 把 token 片段误当成 host 去解析
    （现象：Could not resolve host: 8752792005）。
    """
    if not repo_url:
        return ""
    if settings.gitlab_user and settings.gitlab_token:
        token = settings.gitlab_token.get_secret_value()
        # 处理 http(s)://host/path 格式
        if "://" in repo_url:
            scheme, rest = repo_url.split("://", 1)
            user = quote(settings.gitlab_user.strip(), safe="")
            token_enc = quote(token, safe="")
            return f"{scheme}://{user}:{token_enc}@{rest}"
    return repo_url


async def _run_ssh(settings: Settings, command: str, timeout: int = 60) -> tuple[int, str, str]:
    """在目标服务器上执行 SSH 命令。

    用 asyncio.to_thread 包 paramiko 同步调用，避免阻塞事件循环。
    测试 monkeypatch 切入点：mock deploy_agent.tools._run_ssh 即可模拟 SSH 结果。

    Returns:
        (exit_code, stdout, stderr)
    """
    def _execute() -> tuple[int, str, str]:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=settings.server_host,
                port=settings.server_port,
                username=settings.server_user,
                password=(
                    settings.server_password.get_secret_value()
                    if settings.server_password
                    else None
                ),
                timeout=timeout,
            )
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return (
                exit_code,
                stdout.read().decode("utf-8", errors="replace"),
                stderr.read().decode("utf-8", errors="replace"),
            )
        finally:
            client.close()

    return await asyncio.to_thread(_execute)


# 实时构建日志通道（ContextVar 实现）：
# api.py 的 SSE 消费者在创建 producer task 前 set 一个 asyncio.Queue，
# producer task（含工具执行）复制了该上下文，build_docker_image 在工具内读取
# ContextVar 拿到队列，把 docker build 的每一行增量发布进去；
# 消费者并发排空该队列并转发为 build_log SSE 事件。
_build_log_queue_var: contextvars.ContextVar[asyncio.Queue | None] = contextvars.ContextVar(
    "deploy_build_log_queue", default=None
)


def set_build_log_queue(queue: asyncio.Queue | None) -> None:
    """为当前请求设置/清除实时构建日志队列（SSE 消费者侧调用）。"""
    _build_log_queue_var.set(queue)


def get_build_log_queue() -> asyncio.Queue | None:
    """读取当前请求的构建日志队列（工具侧调用，与消费者同一队列）。"""
    return _build_log_queue_var.get()


async def _run_ssh_stream(
    settings: Settings,
    command: str,
    timeout: int = 600,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """在目标服务器上执行 SSH 命令，stdout 逐行增量回调。

    与 _run_ssh 的区别：
    - stdout 边读边回调 on_line（经 loop.call_soon_threadsafe 切回事件循环线程，
      保证 asyncio.Queue.put_nowait / Future.set_result 在线程安全的前提下被调用）
    - 用于 docker build 等长耗时命令的实时进度推送（命令需自带 2>&1 合并 stderr）
    - asyncio.to_thread 会携带调用方上下文执行，因此 on_line 闭包里
      读取 ContextVar（get_build_log_queue）拿到的就是当前请求的队列

    Returns:
        (exit_code, stdout, stderr) 与 _run_ssh 一致
    """
    loop = asyncio.get_running_loop()

    def _execute() -> tuple[int, str, str]:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=settings.server_host,
                port=settings.server_port,
                username=settings.server_user,
                password=(
                    settings.server_password.get_secret_value()
                    if settings.server_password
                    else None
                ),
                timeout=timeout,
            )
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out_lines: list[str] = []
            # paramiko ChannelFile 迭代按行产出 str（文本模式 readline），
            # 个别版本/环境可能产出 bytes 块，这里兼容两种形态；
            # 同时用 buf 按 \n 切分，保证 on_line 回调的是完整行而非块片段
            buf = ""
            for raw in stdout:
                text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                buf += text
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    out_lines.append(line + "\n")
                    if on_line is not None:
                        loop.call_soon_threadsafe(on_line, line)
            if buf:
                out_lines.append(buf)
                if on_line is not None:
                    loop.call_soon_threadsafe(on_line, buf.rstrip("\n"))
            exit_code = stdout.channel.recv_exit_status()
            return exit_code, "".join(out_lines), ""
        finally:
            client.close()

    return await asyncio.to_thread(_execute)


def _cmd_summary(cmd: str, max_len: int = 200) -> str:
    """命令摘要（截断，不含密码）。"""
    cmd = cmd.replace("\n", " ").strip()
    if len(cmd) > max_len:
        return cmd[:max_len] + "..."
    return cmd


async def _find_dockerfile(
    settings: Settings, code_path: str, tool_name: str
) -> tuple[bool, str]:
    """SSH 依次检查 code_path 根目录与 docker/ 子目录下的 Dockerfile。

    check_dockerfile 与 build_docker_image 共用此逻辑，保证两处判定一致。
    一条组合命令查两个位置，避免两次 SSH 往返。

    Returns:
        (has_dockerfile, found_path)：found_path 为 Dockerfile 完整路径，未找到时为空串
    """
    cmd = (
        f"if [ -f {code_path}/Dockerfile ]; then echo FOUND:{code_path}/Dockerfile; "
        f"elif [ -f {code_path}/docker/Dockerfile ]; then "
        f"echo FOUND:{code_path}/docker/Dockerfile; "
        f"else echo NOTFOUND; fi"
    )
    logger.info(
        "tool={} | host={} | cmd={}",
        tool_name,
        settings.server_host,
        _cmd_summary(cmd),
    )
    _, out, _ = await _run_ssh(settings, cmd)
    out = out.strip()
    if out.startswith("FOUND:"):
        return True, out[len("FOUND:"):]
    return False, ""


def _parse_ls_output(output: str) -> list[dict]:
    """解析 `ls -la --time-style=long-iso` 输出为结构化列表。

    输入示例：
        total 8
        drwxr-xr-x 2 root root 4.0K 2026-08-20 10:30 .
        -rw-r--r-- 1 root root 1.2K 2026-08-20 10:30 Dockerfile

    用 maxsplit=7 拆分：perms links owner group size date time name
    文件名含空格时整段归到第 8 段（maxsplit 保证不丢数据）。
    跳过 total 行和 . / .. 条目。
    """
    files: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("total "):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        perms, _links, owner, group, size, date, _time, name = parts
        if name in (".", ".."):
            continue
        files.append(
            {
                "name": name,
                "type": "dir" if perms.startswith("d") else "file",
                "size": size,
                "modified": f"{date} {_time}",
                "perms": perms,
                "owner": owner,
                "group": group,
            }
        )
    return files


def _validate_workspace_subpath(
    workspace: str,
    file_path: str,
    settings: Settings,
    protect_git: bool = False,
) -> tuple[bool, str]:
    """校验 workspace 在白名单内 + file_path 安全（相对路径、无 .. 段）。

    read 调用 protect_git=False，write/delete 调用 protect_git=True
    防止破坏 .git 版本控制数据。

    Returns:
        (ok, error_message)：ok=True 时 error_message 为空
    """
    if not settings.workspaces or workspace not in settings.workspaces:
        return False, f"workspace 不在白名单内，仅允许 {settings.workspaces}"
    if not file_path or not file_path.strip():
        return False, "file_path 不能为空"
    # 禁止绝对路径：防止 file_path 为 /etc/passwd 这种绕过 workspace 拼接
    if file_path.startswith("/"):
        return False, "file_path 必须是相对路径，禁止绝对路径"
    # 禁止 .. 段：防止 workspace/../../etc 路径逃逸
    segments = file_path.replace("\\", "/").split("/")
    if any(seg == ".." for seg in segments):
        return False, "file_path 禁止包含 ..（防止越权）"
    # write/delete 额外保护 .git 目录
    if protect_git:
        normalized = file_path.replace("\\", "/")
        if normalized == ".git" or normalized.startswith(".git/"):
            return False, "禁止操作 .git 目录（防止破坏版本控制）"
    return True, ""


# 白名单增删的全局锁：防止并发请求同时写 whitelist.json 互相覆盖
_whitelist_lock = asyncio.Lock()

# 白名单 scope → (settings 上的 raw 字段名, 列表属性名, 展示名)
_WHITELIST_SCOPES: dict[str, tuple[str, str, str]] = {
    "container": ("container_names_raw", "container_names", "容器名"),
    "image": ("image_prefixes_raw", "image_prefixes", "镜像前缀"),
}


def _persist_whitelist_json(settings: Settings, path: Path) -> None:
    """把当前内存中的容器/镜像白名单写入 whitelist.json。

    两个 scope 必须同时写入，避免只更新一个导致另一个在 JSON 里丢失。
    """
    data = {
        "container_names": settings.container_names,
        "image_prefixes": settings.image_prefixes,
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ==================== 工具构建器 ====================


def build_git_pull_code_tool(settings: Settings):
    """构建 git_pull_code 工具。

    校验：workspace 必须在 settings.workspaces 白名单内
    repo_url/branch 由用户在对话中指定，不做白名单校验
    SSH 执行：clone（首次）或 checkout + pull；再 rev-parse 取 commit
    返回：{success, branch, commit}
    """

    @tool
    async def git_pull_code(repo_url: str, branch: str, workspace: str) -> str:
        """拉取目标仓库指定分支最新代码，返回 commit hash。

        Args:
            repo_url: 仓库地址（由用户在对话中指定，无白名单）
            branch: 目标分支（由用户在对话中指定，无白名单）
            workspace: 目标服务器上的工作目录路径（必须在 settings.workspaces 白名单内）
        """
        tool_name = "git_pull_code"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=repo_url={} branch={} workspace={}",
            tool_name,
            _mask(repo_url),
            branch,
            workspace,
        )

        # 参数校验：workspace 必须在白名单内
        # repo_url/branch 不做白名单（由用户在对话中指定），仍受 workspace 白名单约束防止越权写入
        if not settings.workspaces or workspace not in settings.workspaces:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=workspace_not_allowed | elapsed={}ms", tool_name, elapsed
            )
            return _err(
                "validation_error",
                f"workspace 不在白名单内，仅允许 {settings.workspaces}",
                workspace=workspace,
            )

        try:
            authed_url = _build_repo_url_with_auth(repo_url, settings)
            # 判断 workspace 是否已是 git 仓库（不只是目录是否存在）。
            # 用 `git -C {workspace} rev-parse --is-inside-work-tree` 并把输出严格比对为 "true"：
            # - 老版本 git 在非仓库目录下该命令可能输出 "false" 且 exit 0，
            #   旧逻辑只看 exit code + 字符串 EXISTS，导致误判为已存在仓库，
            #   随后 checkout 报 not a git repository 且永不走 clone 分支。
            # - 兼容 worktree 等非常规 clone 形式（.git 可能是文件而非目录）
            # - 2>/dev/null 抑制非 git 目录下的报错输出，避免污染日志
            detect_cmd = (
                f"git -C {workspace} rev-parse --is-inside-work-tree 2>/dev/null"
            )
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(detect_cmd),
            )
            _, out, _ = await _run_ssh(settings, detect_cmd)
            is_repo = out.strip() == "true"

            async def run_clone() -> tuple[int, str, str]:
                """首次克隆（命令含鉴权，日志只记录摘要不含 URL）。"""
                clone_cmd = (
                    f"git clone {authed_url} {workspace} && "
                    f"cd {workspace} && git checkout {branch}"
                )
                logger.info(
                    "tool={} | path=clone | host={} | cmd=git clone ... {}",
                    tool_name,
                    settings.server_host,
                    workspace,
                )
                ec, so, se = await _run_ssh(settings, clone_cmd)
                # 记录 git 命令执行结果摘要（clone 输出形如 "Cloning into 'xxx'..."，不含 token，可安全打印）
                logger.info(
                    "tool={} | cmd_done | exit={} | stdout={} | stderr={}",
                    tool_name,
                    ec,
                    _cmd_summary(so),
                    _cmd_summary(se),
                )
                return ec, so, se

            if is_repo:
                # 记录分支选择，便于排查时一眼识别本次部署路径
                logger.info(
                    "tool={} | path=pull | workspace 已是 git 仓库，执行增量拉取",
                    tool_name,
                )
                # 已存在：checkout + pull
                pull_cmd = (
                    f"cd {workspace} && git checkout {branch} && git pull"
                )
                logger.info(
                    "tool={} | host={} | cmd={}",
                    tool_name,
                    settings.server_host,
                    _cmd_summary(pull_cmd),
                )
                exit_code, out, err = await _run_ssh(settings, pull_cmd)
                # 记录 git 命令执行结果摘要，无论成功失败都有用：
                # - 成功路径：stdout 含 "Already up to date" / "Updating abc..def" 等关键信息
                # - 失败路径：与下方 error 日志互补，便于复盘
                logger.info(
                    "tool={} | cmd_done | exit={} | stdout={} | stderr={}",
                    tool_name,
                    exit_code,
                    _cmd_summary(out),
                    _cmd_summary(err),
                )
                # 兜底：探测误判为仓库、实际并非 git 仓库时（checkout 报 not a git repository），
                # 回退到首次克隆，而不是直接失败
                if exit_code != 0 and "not a git repository" in err:
                    logger.warning(
                        "tool={} | path=pull_fallback_to_clone | 误判为仓库，回退首次克隆",
                        tool_name,
                    )
                    exit_code, out, err = await run_clone()
            else:
                # 记录分支选择，便于排查时一眼识别本次部署路径
                logger.info(
                    "tool={} | path=clone | workspace 非 git 仓库，执行首次克隆",
                    tool_name,
                )
                exit_code, out, err = await run_clone()

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=git_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "git 操作失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            # 获取 commit hash
            rev_cmd = f"cd {workspace} && git rev-parse --short HEAD"
            _, commit_out, _ = await _run_ssh(settings, rev_cmd)
            commit = commit_out.strip()

            elapsed = _elapsed_ms(start)
            # commit 加入 ok 日志，便于 grep 排查部署前后 commit 变化
            logger.info(
                "tool={} | ok | commit={} | elapsed={}ms",
                tool_name,
                commit,
                elapsed,
            )
            return _ok(branch=branch, commit=commit)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return git_pull_code


def build_docker_image_tool(settings: Settings):
    """构建 build_docker_image 工具。

    白名单自动加入：image_name 不以已有前缀开头时，自动将 image_name 加入镜像前缀白名单
    SSH 执行：docker build -t name:tag code_path，完整 stdout+stderr 存 log 字段
    返回：{success, image, log}
    """

    @tool
    async def build_docker_image(code_path: str, image_name: str, image_tag: str) -> str:
        """构建 Docker 镜像，返回镜像全名。
        镜像名不以已有前缀开头时，自动将镜像名加入前缀白名单。

        Args:
            code_path: 代码路径（含 Dockerfile）
            image_name: 镜像名（不以已有前缀开头时自动加入白名单）
            image_tag: 镜像 tag
        """
        tool_name = "build_docker_image"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=code_path={} image_name={} image_tag={}",
            tool_name,
            code_path,
            image_name,
            image_tag,
        )

        # image_name 合法性校验（与 add_whitelist_entry 一致）
        if not image_name or not image_name.strip():
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=empty_image_name | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", "image_name 不能为空")
        # 逗号是 image_prefixes_raw 的多值分隔符，含逗号会破坏格式
        if "," in image_name:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=comma_in_image_name | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", "image_name 不能包含逗号")

        # 镜像前缀不在白名单内时自动加入，免去手动调 add_whitelist_entry
        prefixes = settings.image_prefixes
        if not prefixes or not any(image_name.startswith(p) for p in prefixes):
            logger.info(
                "tool={} | image_name={} 不以已有前缀开头，自动加入 | current={}",
                tool_name,
                image_name,
                prefixes,
            )
            async with _whitelist_lock:
                # 二次检查防竞态
                current_prefixes = settings.image_prefixes
                if not any(image_name.startswith(p) for p in current_prefixes):
                    new_prefixes = current_prefixes + [image_name]
                    old_raw = settings.image_prefixes_raw
                    try:
                        # 先改内存再写盘，写盘失败则回滚内存
                        setattr(settings, "image_prefixes_raw", ",".join(new_prefixes))
                        _persist_whitelist_json(settings, settings.whitelist_path)
                        logger.info(
                            "tool={} | image_name={} 已加入前缀白名单 | whitelist={}",
                            tool_name,
                            image_name,
                            new_prefixes,
                        )
                    except OSError as e:
                        # 回滚内存，保证内存与磁盘一致
                        setattr(settings, "image_prefixes_raw", old_raw)
                        elapsed = _elapsed_ms(start)
                        logger.error(
                            "tool={} | error=auto_whitelist_failed | elapsed={}ms",
                            tool_name,
                            elapsed,
                        )
                        return _err(
                            "persist_failed",
                            f"自动加入前缀白名单失败: {e}",
                            image_name=image_name,
                        )

        try:
            # docker build 前先确认 Dockerfile 存在（与 check_dockerfile 同一套逻辑）
            # 缺失时直接返回友好错误，不执行 docker build，避免无意义的失败构建
            has_df, df_path = await _find_dockerfile(settings, code_path, tool_name)
            if not has_df:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=dockerfile_missing | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "dockerfile_missing",
                    f"{code_path} 缺少 Dockerfile，需要先添加 Dockerfile 才能打包",
                    code_path=code_path,
                )

            full_image = f"{image_name}:{image_tag}"
            # Dockerfile 在 docker/ 子目录时用 -f 显式指定；
            # build context 保持 code_path（项目根），COPY 指令按项目根相对路径解析
            # --progress=plain：非 TTY 下逐行输出构建步骤（默认 fancy 模式会整块缓冲）
            # 2>&1：stdout+stderr 合并，_run_ssh_stream 才能逐行实时读到全部输出
            if df_path == f"{code_path}/Dockerfile":
                cmd = f"docker build --progress=plain -t {full_image} {code_path} 2>&1"
            else:
                cmd = (
                    f"docker build --progress=plain -t {full_image} "
                    f"-f {df_path} {code_path} 2>&1"
                )
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            # 实时构建日志：每一行经 ContextVar 队列发布到当前请求的 SSE 消费者
            build_queue = get_build_log_queue()

            def _publish_build_line(line: str) -> None:
                if build_queue is not None and line:
                    build_queue.put_nowait(("__build_log__", tool_name, line))

            # docker build 可能较慢，给 10 分钟；stdout 逐行回调推送
            exit_code, out, err = await _run_ssh_stream(
                settings, cmd, timeout=600, on_line=_publish_build_line
            )

            # 完整 stdout（已含 stderr）存入 log 字段
            log_text = (out + ("\n" + err if err.strip() else "")).strip()

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=build_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker build 失败",
                    exit_code=exit_code,
                    log=log_text,
                )

            elapsed = _elapsed_ms(start)
            logger.info("tool={} | ok | elapsed={}ms", tool_name, elapsed)
            return _ok(image=full_image, log=log_text)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return build_docker_image


def build_stop_container_tool(settings: Settings):
    """构建 stop_container 工具。

    校验：container_name 必须在 settings.container_names 白名单内
    SSH 执行：docker stop
    返回：{success, container_name}
    """

    @tool
    async def stop_container(container_name: str) -> str:
        """停止指定容器。

        Args:
            container_name: 容器名（必须在 settings.container_names 白名单内）
        """
        tool_name = "stop_container"
        start = time.perf_counter()
        logger.info("tool={} | args=container_name={}", tool_name, container_name)

        # 参数校验：必须在白名单内
        if not settings.container_names or container_name not in settings.container_names:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=container_not_allowed | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"container_name 不在白名单内，仅允许 {settings.container_names}",
                container_name=container_name,
            )

        try:
            cmd = f"docker stop {container_name}"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            exit_code, out, err = await _run_ssh(settings, cmd)

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=stop_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker stop 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            elapsed = _elapsed_ms(start)
            logger.info("tool={} | ok | elapsed={}ms", tool_name, elapsed)
            return _ok(container_name=container_name)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return stop_container


def build_remove_container_tool(settings: Settings):
    """构建 remove_container 工具（删容器，需审批）。

    校验：container_name 必须在 settings.container_names 白名单内
    SSH 执行：docker rm -f（强制删除，含运行中的容器）
    返回：{success, container_name}
    与 start_container 解耦：删除不再隐含在启动里，用户审批时意图明确
    """

    @tool
    async def remove_container(container_name: str) -> str:
        """删除指定容器（强制删除，含运行中的容器）。触发人工审批。

        Args:
            container_name: 容器名（必须在 settings.container_names 白名单内）
        """
        tool_name = "remove_container"
        start = time.perf_counter()
        logger.info("tool={} | args=container_name={}", tool_name, container_name)

        # 参数校验：必须在白名单内
        if not settings.container_names or container_name not in settings.container_names:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=container_not_allowed | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"container_name 不在白名单内，仅允许 {settings.container_names}",
                container_name=container_name,
            )

        try:
            cmd = f"docker rm -f {container_name}"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            exit_code, out, err = await _run_ssh(settings, cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=rm_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker rm 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            elapsed = _elapsed_ms(start)
            logger.info("tool={} | ok | elapsed={}ms", tool_name, elapsed)
            return _ok(container_name=container_name)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return remove_container


def build_start_container_tool(settings: Settings):
    """构建 start_container 工具（启动新容器，需审批）。

    白名单自动加入：container_name 不在白名单内时自动添加（内存+whitelist.json 持久化）
    校验：image 以 IMAGE_PREFIX 列表中任一前缀开头
    前置检查：同名容器若已存在则拒绝（需先调 remove_container 删除），避免隐式覆盖
    SSH 执行：docker run -d --name（不再内含 docker rm -f）
    返回：{success, container_name, image}
    """

    @tool
    async def start_container(container_name: str, image: str) -> str:
        """启动新容器。若同名容器已存在则拒绝，需先调 remove_container 删除。
        容器名不在白名单内时自动加入白名单。

        Args:
            container_name: 容器名（不在白名单时自动加入）
            image: 镜像全名（必须以 IMAGE_PREFIX 中任意一个前缀开头）
        """
        tool_name = "start_container"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=container_name={} image={}",
            tool_name,
            container_name,
            _mask(image),
        )

        # 容器名合法性校验（与 add_whitelist_entry 一致）
        if not container_name or not container_name.strip():
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=empty_container_name | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", "container_name 不能为空")
        # 逗号是 container_names_raw 的多值分隔符，含逗号会破坏格式
        if "," in container_name:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=comma_in_container_name | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", "container_name 不能包含逗号")

        # 容器名不在白名单内时自动加入，免去手动调 add_whitelist_entry
        if not settings.container_names or container_name not in settings.container_names:
            logger.info(
                "tool={} | container_name={} 不在白名单内，自动加入 | current={}",
                tool_name,
                container_name,
                settings.container_names,
            )
            async with _whitelist_lock:
                # 二次检查防竞态：锁内再判一次，避免并发请求重复添加
                current = settings.container_names
                if container_name not in current:
                    new_list = current + [container_name]
                    old_raw = settings.container_names_raw
                    try:
                        # 先改内存再写盘，写盘失败则回滚内存
                        setattr(settings, "container_names_raw", ",".join(new_list))
                        _persist_whitelist_json(settings, settings.whitelist_path)
                        logger.info(
                            "tool={} | container_name={} 已加入白名单 | whitelist={}",
                            tool_name,
                            container_name,
                            new_list,
                        )
                    except OSError as e:
                        # 回滚内存，保证内存与磁盘一致
                        setattr(settings, "container_names_raw", old_raw)
                        elapsed = _elapsed_ms(start)
                        logger.error(
                            "tool={} | error=auto_whitelist_failed | elapsed={}ms",
                            tool_name,
                            elapsed,
                        )
                        return _err(
                            "persist_failed",
                            f"自动加入白名单失败: {e}",
                            container_name=container_name,
                        )
        # 参数校验：镜像前缀（多值白名单，命中任一即可）
        prefixes = settings.image_prefixes
        if not prefixes or not any(image.startswith(p) for p in prefixes):
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=image_prefix_not_allowed | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"image 必须以以下前缀之一开头：{prefixes}",
                image=image,
            )

        try:
            # 前置检查：同名容器若已存在则拒绝，避免隐式覆盖
            # docker ps -a --filter name=^/{name}$ --format {{.Names}} 精确匹配
            check_cmd = (
                f"docker ps -a --filter name=^/{container_name}$ "
                f"--format '{{{{.Names}}}}'"
            )
            logger.info(
                "tool={} | host={} | check_cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(check_cmd),
            )
            exit_code, out, err = await _run_ssh(settings, check_cmd)
            logger.info(
                "tool={} | check ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )
            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=check_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "检查同名容器失败",
                    exit_code=exit_code,
                    stderr=err,
                )
            # 输出非空说明同名容器已存在
            if out.strip():
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=container_already_exists | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "container_already_exists",
                    f"容器 {container_name} 已存在，请先调用 remove_container 删除",
                    container_name=container_name,
                )

            # 启动新容器（不再内含 docker rm -f）
            run_cmd = f"docker run -d --name {container_name} {image}"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(run_cmd),
            )
            exit_code, out, err = await _run_ssh(settings, run_cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=start_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker run 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            elapsed = _elapsed_ms(start)
            logger.info("tool={} | ok | elapsed={}ms", tool_name, elapsed)
            return _ok(container_name=container_name, image=image)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return start_container


def build_check_service_health_tool(settings: Settings):
    """构建 check_service_health 工具。

    SSH 执行：docker inspect 容器状态 + curl health_url
    返回：{success, status, container_status, http_status}
    """

    @tool
    async def check_service_health(
        container_name: str, health_url: str | None = None
    ) -> str:
        """检查容器运行状态 + HTTP 健康检查。

        Args:
            container_name: 容器名
            health_url: 健康检查 URL（缺省取 settings.health_url）
        """
        tool_name = "check_service_health"
        start = time.perf_counter()
        # health_url 缺省取 settings.health_url
        url = health_url or settings.health_url or ""
        logger.info(
            "tool={} | args=container_name={} health_url={}",
            tool_name,
            container_name,
            url,
        )

        try:
            # docker inspect 容器状态
            # Go template {{.State.Status}} 在 f-string 中需要双花括号转义
            inspect_cmd = (
                f"docker inspect -f '{{{{.State.Status}}}}' {container_name}"
            )
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(inspect_cmd),
            )
            exit_code, out, err = await _run_ssh(settings, inspect_cmd)
            container_status = out.strip() if exit_code == 0 else "unknown"

            # curl health_url
            if url:
                # curl 的 %{http_code} 在 f-string 中需要双花括号转义
                curl_cmd = (
                    f"curl -s -o /dev/null -w '%{{http_code}}' {url}"
                )
                logger.info(
                    "tool={} | host={} | cmd={}",
                    tool_name,
                    settings.server_host,
                    _cmd_summary(curl_cmd),
                )
                exit_code2, out2, err2 = await _run_ssh(settings, curl_cmd)
                http_status = out2.strip() if exit_code2 == 0 else "000"
            else:
                http_status = "no_health_url"

            # 综合判定：容器 running + HTTP 200/204 = healthy
            healthy = container_status == "running" and http_status in ("200", "204")
            elapsed = _elapsed_ms(start)
            logger.info("tool={} | ok | elapsed={}ms", tool_name, elapsed)
            return _ok(
                status="healthy" if healthy else "unhealthy",
                container_status=container_status,
                http_status=http_status,
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return check_service_health


def build_list_containers_tool(settings: Settings):
    """构建 list_containers 工具（只读，无白名单校验，不入审批名单）。

    SSH 执行：docker ps [--format]，include_all=True 时加 -a 含已停止容器
    返回：{success, containers:[{name, image, status, ports}], count}
    """

    @tool
    async def list_containers(include_all: bool = False) -> str:
        """列出目标服务器上的容器。

        Args:
            include_all: True 含已停止的容器，False 只看运行中的容器（默认）
        """
        tool_name = "list_containers"
        start = time.perf_counter()
        logger.info("tool={} | args=include_all={}", tool_name, include_all)

        try:
            # docker format 模板里的 {{.Xxx}} 在 f-string 中需要双花括号转义
            fmt = r"{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"
            # include_all=True 时 -a 含已停止容器
            flag = "-a " if include_all else ""
            cmd = f"docker ps {flag}--format '{fmt}'"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            exit_code, out, err = await _run_ssh(settings, cmd)

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=ps_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker ps 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            containers = []
            for line in out.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                # 用 maxsplit=3：前 3 段固定，剩余全归 ports（即使 ports 含分隔符也不丢数据）
                parts = line.split("|", 3)
                if len(parts) != 4:
                    continue
                containers.append(
                    {
                        "name": parts[0],
                        "image": parts[1],
                        "status": parts[2],
                        "ports": parts[3],
                    }
                )

            elapsed = _elapsed_ms(start)
            # list 类只记条数，不记全量内容，避免刷屏
            logger.info(
                "tool={} | count={} | elapsed={}ms", tool_name, len(containers), elapsed
            )
            return _ok(containers=containers, count=len(containers))
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return list_containers


def build_list_images_tool(settings: Settings):
    """构建 list_images 工具（只读，无白名单校验，不入审批名单）。

    SSH 执行：docker images --format
    返回：{success, images:[{image, id, size}], count}
    """

    @tool
    async def list_images() -> str:
        """列出目标服务器上的 Docker 镜像。"""
        tool_name = "list_images"
        start = time.perf_counter()
        logger.info("tool={} | args=<none>", tool_name)

        try:
            # docker format 模板里的 {{.Xxx}} 在 f-string 中需要双花括号转义
            fmt = r"{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}"
            cmd = f"docker images --format '{fmt}'"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            exit_code, out, err = await _run_ssh(settings, cmd)

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=images_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "docker images 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            images = []
            for line in out.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) != 3:
                    continue
                images.append(
                    {"image": parts[0], "id": parts[1], "size": parts[2]}
                )

            elapsed = _elapsed_ms(start)
            # list 类只记条数，不记全量内容，避免刷屏
            logger.info(
                "tool={} | count={} | elapsed={}ms", tool_name, len(images), elapsed
            )
            return _ok(images=images, count=len(images))
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return list_images


def build_check_dockerfile_tool(settings: Settings):
    """构建 check_dockerfile 工具（只读，不入审批名单）。

    校验：code_path 必须在 settings.workspaces 白名单内
    SSH 执行：依次检查 {code_path}/Dockerfile 与 {code_path}/docker/Dockerfile
    返回：{success, has_dockerfile, found_path, hint}
    """

    @tool
    async def check_dockerfile(code_path: str) -> str:
        """检查指定代码目录是否存在 Dockerfile。

        Args:
            code_path: 代码目录路径（必须在 settings.workspaces 白名单内）
        """
        tool_name = "check_dockerfile"
        start = time.perf_counter()
        logger.info("tool={} | args=code_path={}", tool_name, code_path)

        # 参数校验：code_path 必须在 workspace 白名单内（与 git_pull_code 同一套白名单）
        if not settings.workspaces or code_path not in settings.workspaces:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=workspace_not_allowed | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"code_path 不在白名单内，仅允许 {settings.workspaces}",
                code_path=code_path,
            )

        try:
            has_df, df_path = await _find_dockerfile(settings, code_path, tool_name)
            elapsed = _elapsed_ms(start)
            if has_df:
                logger.info(
                    "tool={} | ok | elapsed={}ms", tool_name, elapsed
                )
                return _ok(
                    has_dockerfile=True,
                    found_path=df_path,
                    hint=f"Dockerfile 位于 {df_path}",
                )
            # 检查本身成功但未找到 Dockerfile：success=True + has_dockerfile=False + hint 引导
            logger.info(
                "tool={} | ok | elapsed={}ms", tool_name, elapsed
            )
            return _ok(
                has_dockerfile=False,
                found_path="",
                hint="该目录缺少 Dockerfile，需要先添加 Dockerfile 才能打包",
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return check_dockerfile


def build_list_workspace_files_tool(settings: Settings):
    """构建 list_workspace_files 工具（只读巡检）。

    校验：workspace 必须在 settings.workspaces 白名单内（防越权列任意目录）
    安全：subdir 禁止 .. 段（防止 workspace/../../etc 逃逸）
    SSH 执行：ls -la --time-style=long-iso {target}（-a 含隐藏文件）
    返回：{success, workspace, subdir, target, files:[{name,type,size,modified,perms,owner,group}], count}
    注：不再返回 raw（原始 ls 输出）。files 已含全部结构化信息，raw 既冗余又会被
    LLM 原样复述进对话文本（前端卡片与消息区都会显示），故移除。
    """

    @tool
    async def list_workspace_files(workspace: str, subdir: str = "") -> str:
        """列出目标服务器上 workspace 目录内的文件（含隐藏文件与详细信息）。

        Args:
            workspace: 工作目录路径（必须在 settings.workspaces 白名单内）
            subdir: 子目录（可选，相对 workspace，禁止 .. 越权）
        """
        tool_name = "list_workspace_files"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=workspace={} subdir={}",
            tool_name,
            workspace,
            subdir,
        )

        # 白名单校验：workspace 必须命中，防越权列任意目录
        if not settings.workspaces or workspace not in settings.workspaces:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=workspace_not_allowed | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"workspace 不在白名单内，仅允许 {settings.workspaces}",
                workspace=workspace,
            )

        # subdir 安全过滤：路径段任一为 .. 即拒绝
        # 防止 workspace/../../etc 这种路径逃逸到白名单之外
        if subdir:
            segments = subdir.replace("\\", "/").split("/")
            if any(seg == ".." for seg in segments):
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=subdir_traversal_blocked | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "validation_error",
                    "subdir 禁止包含 ..（防止越权）",
                    subdir=subdir,
                )

        # 拼接最终路径：subdir 为空时只列 workspace 根目录
        target = f"{workspace}/{subdir}" if subdir else workspace
        logger.info(
            "tool={} | validation passed | target={}",
            tool_name,
            target,
        )

        try:
            # -a 含隐藏文件（.git/.env 等），--time-style=long-iso 固定时间格式便于解析
            cmd = f"ls -la --time-style=long-iso {target}"
            logger.info(
                "tool={} | host={} | cmd={}",
                tool_name,
                settings.server_host,
                _cmd_summary(cmd),
            )
            exit_code, out, err = await _run_ssh(settings, cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )

            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=ls_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "ls 失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            files = _parse_ls_output(out)
            elapsed = _elapsed_ms(start)
            logger.info(
                "tool={} | count={} | elapsed={}ms",
                tool_name,
                len(files),
                elapsed,
            )
            return _ok(
                workspace=workspace,
                subdir=subdir,
                target=target,
                files=files,
                count=len(files),
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return list_workspace_files


def build_read_workspace_file_tool(settings: Settings):
    """构建 read_workspace_file 工具（只读巡检，无审批）。

    校验：workspace 白名单 + file_path 相对路径无 ..（protect_git=False）
    SSH 执行：组合命令查存在性/大小/cat（>1MB 拒绝）
    返回：{success, content, size, target, truncated}
    """

    @tool
    async def read_workspace_file(workspace: str, file_path: str) -> str:
        """读取 workspace 内文件内容。

        Args:
            workspace: 工作目录路径（必须在 settings.workspaces 白名单内）
            file_path: 文件相对路径（相对 workspace，禁止绝对路径和 ..）
        """
        tool_name = "read_workspace_file"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=workspace={} file_path={}",
            tool_name,
            workspace,
            file_path,
        )

        ok, msg = _validate_workspace_subpath(
            workspace, file_path, settings, protect_git=False
        )
        if not ok:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=validation_error | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", msg, workspace=workspace)

        target = f"{workspace}/{file_path}"
        q = shlex.quote(target)
        logger.info(
            "tool={} | validation passed | target={}",
            tool_name,
            target,
        )
        # 组合命令：不存在 → __NOTEXISTS__；超大 → __TOOLARGE__+size；否则 cat 内容
        # stat -c %s 取字节数，1048576 = 1MB
        cmd = (
            f"if [ ! -f {q} ]; then echo '__NOTEXISTS__'; "
            f"elif [ \"$(stat -c %s {q} 2>/dev/null || echo 0)\" -gt 1048576 ]; then "
            f"echo \"__TOOLARGE__$(stat -c %s {q} 2>/dev/null || echo 0)\"; "
            f"else cat {q}; fi"
        )
        logger.info(
            "tool={} | host={} | cmd={}",
            tool_name,
            settings.server_host,
            _cmd_summary(cmd),
        )
        try:
            exit_code, out, err = await _run_ssh(settings, cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )
            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=cat_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "读取文件失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            stripped = out.strip()
            if stripped == "__NOTEXISTS__":
                elapsed = _elapsed_ms(start)
                logger.info(
                    "tool={} | not_found | elapsed={}ms", tool_name, elapsed
                )
                return _err("not_found", "文件不存在", target=target)
            if stripped.startswith("__TOOLARGE__"):
                size_str = stripped[len("__TOOLARGE__"):]
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=file_too_large size={} | elapsed={}ms",
                    tool_name,
                    size_str,
                    elapsed,
                )
                return _err(
                    "file_too_large",
                    f"文件超过 1MB 限制（{size_str} 字节）",
                    size=int(size_str) if size_str.isdigit() else 0,
                )

            elapsed = _elapsed_ms(start)
            logger.info(
                "tool={} | ok size={} | elapsed={}ms",
                tool_name,
                len(out),
                elapsed,
            )
            return _ok(
                content=out,
                size=len(out),
                target=target,
                truncated=False,
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return read_workspace_file


def build_write_workspace_file_tool(settings: Settings):
    """构建 write_workspace_file 工具（写操作，需审批）。

    校验：workspace 白名单 + file_path 相对路径无 ..（protect_git=True，禁写 .git/）
    限制：content ≤ 256KB（base64 后约 341KB，远低于 ARG_MAX）
    SSH 执行：mkdir -p 父目录 + echo {b64} | base64 -d > target（base64 防注入）
    返回：{success, target, bytes_written}
    """

    @tool
    async def write_workspace_file(
        workspace: str, file_path: str, content: str
    ) -> str:
        """写入文件（覆盖）。触发人工审批。

        Args:
            workspace: 工作目录路径（必须在 settings.workspaces 白名单内）
            file_path: 文件相对路径（禁止绝对路径、..、.git/ 下文件）
            content: 文件内容（≤256KB）
        """
        tool_name = "write_workspace_file"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=workspace={} file_path={} content_len={}",
            tool_name,
            workspace,
            file_path,
            len(content) if content else 0,
        )

        ok, msg = _validate_workspace_subpath(
            workspace, file_path, settings, protect_git=True
        )
        if not ok:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=validation_error | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", msg, workspace=workspace)

        # 大小限制 256KB
        encoded = content.encode("utf-8") if content else b""
        if len(encoded) > 256 * 1024:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=content_too_large | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "content_too_large",
                f"content 超过 256KB 限制（{len(encoded)} 字节）",
            )

        target = f"{workspace}/{file_path}"
        q = shlex.quote(target)
        logger.info(
            "tool={} | validation passed | target={} | content_bytes={}",
            tool_name,
            target,
            len(encoded),
        )
        # 父目录：target 含 / 时取前缀，否则用 workspace
        parent = target.rsplit("/", 1)[0] if "/" in target else workspace
        parent_q = shlex.quote(parent)
        # base64 编码 content，echo | base64 -d 写入，避免 shell 特殊字符注入
        b64 = base64.b64encode(encoded).decode("ascii")
        cmd = f"mkdir -p {parent_q} && echo {b64} | base64 -d > {q}"
        logger.info(
            "tool={} | host={} | cmd={} (content_len={})",
            tool_name,
            settings.server_host,
            _cmd_summary(cmd),
            len(encoded),
        )
        try:
            exit_code, out, err = await _run_ssh(settings, cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )
            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=write_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "写入文件失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            elapsed = _elapsed_ms(start)
            logger.info(
                "tool={} | ok bytes={} | elapsed={}ms",
                tool_name,
                len(encoded),
                elapsed,
            )
            return _ok(
                target=target,
                bytes_written=len(encoded),
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return write_workspace_file


def build_delete_workspace_file_tool(settings: Settings):
    """构建 delete_workspace_file 工具（删操作，需审批）。

    校验：workspace 白名单 + file_path 相对路径无 ..（protect_git=True）
    限制：只删文件不删目录（rm -f，遇到目录拒绝）
    SSH 执行：if [ -f ]; then rm -f; elif [ -d ]; then __ISDIR__; else __NOTEXISTS__
    返回：{success, deleted}
    """

    @tool
    async def delete_workspace_file(
        workspace: str, file_path: str
    ) -> str:
        """删除文件（不删目录）。触发人工审批。

        Args:
            workspace: 工作目录路径（必须在 settings.workspaces 白名单内）
            file_path: 文件相对路径（禁止绝对路径、..、.git/ 下文件）
        """
        tool_name = "delete_workspace_file"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=workspace={} file_path={}",
            tool_name,
            workspace,
            file_path,
        )

        ok, msg = _validate_workspace_subpath(
            workspace, file_path, settings, protect_git=True
        )
        if not ok:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=validation_error | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err("validation_error", msg, workspace=workspace)

        target = f"{workspace}/{file_path}"
        q = shlex.quote(target)
        logger.info(
            "tool={} | validation passed | target={}",
            tool_name,
            target,
        )
        # -f 删文件不报错；-d 判断为目录则拒绝（只删文件）
        cmd = (
            f"if [ -f {q} ]; then rm -f {q} && echo '__DELETED__'; "
            f"elif [ -d {q} ]; then echo '__ISDIR__'; "
            f"else echo '__NOTEXISTS__'; fi"
        )
        logger.info(
            "tool={} | host={} | cmd={}",
            tool_name,
            settings.server_host,
            _cmd_summary(cmd),
        )
        try:
            exit_code, out, err = await _run_ssh(settings, cmd)
            logger.info(
                "tool={} | ssh exit={} | out_len={} | err_len={}",
                tool_name,
                exit_code,
                len(out),
                len(err),
            )
            if exit_code != 0:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=rm_failed exit={} | elapsed={}ms",
                    tool_name,
                    exit_code,
                    elapsed,
                )
                return _err(
                    "command_failed",
                    "删除文件失败",
                    exit_code=exit_code,
                    stderr=err,
                )

            stripped = out.strip()
            elapsed = _elapsed_ms(start)
            if stripped == "__DELETED__":
                logger.info(
                    "tool={} | ok | elapsed={}ms", tool_name, elapsed
                )
                return _ok(deleted=target)
            if stripped == "__ISDIR__":
                logger.error(
                    "tool={} | error=is_dir | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "is_directory",
                    "目标是目录，本工具只删文件不删目录",
                    target=target,
                )
            # __NOTEXISTS__
            logger.info(
                "tool={} | not_found | elapsed={}ms", tool_name, elapsed
            )
            return _err("not_found", "文件不存在", target=target)
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error={} | elapsed={}ms",
                tool_name,
                str(e),
                elapsed,
                exc_info=True,
            )
            return _err("ssh_error", f"SSH 执行失败: {e}")

    return delete_workspace_file


def build_add_whitelist_entry_tool(settings: Settings):
    """构建 add_whitelist_entry 工具（白名单变更，需审批）。

    scope ∈ {container, image}：container 追加到 CONTAINER_NAMES，image 追加到 IMAGE_PREFIX
    持久化：更新内存 settings + 写 whitelist.json（重启后 JSON 优先于 .env）
    校验：value 非空、不含逗号（避免破坏多值分隔格式）、不重复
    """

    @tool
    async def add_whitelist_entry(scope: str, value: str) -> str:
        """向白名单添加条目。触发人工审批。

        Args:
            scope: 白名单类型，"container"（容器名）或 "image"（镜像前缀）
            value: 要添加的条目（如容器名 my-app 或镜像前缀 infra/my-app）
        """
        tool_name = "add_whitelist_entry"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=scope={} value={}", tool_name, scope, value
        )

        if scope not in _WHITELIST_SCOPES:
            elapsed = _elapsed_ms(start)
            logger.error(
                "tool={} | error=invalid_scope | elapsed={}ms",
                tool_name,
                elapsed,
            )
            return _err(
                "validation_error",
                f"scope 必须是 {'/'.join(_WHITELIST_SCOPES)} 之一",
                scope=scope,
            )
        if not value or not value.strip():
            return _err("validation_error", "value 不能为空")
        # 逗号是多值分隔符，条目内出现逗号会破坏 raw 字段格式
        if "," in value:
            return _err("validation_error", "value 不能包含逗号")

        raw_field, list_attr, display = _WHITELIST_SCOPES[scope]
        async with _whitelist_lock:
            current: list[str] = getattr(settings, list_attr)
            if value in current:
                elapsed = _elapsed_ms(start)
                logger.info(
                    "tool={} | already_exists | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "already_exists",
                    f"{display} {value} 已在白名单内",
                    whitelist=current,
                )

            new_list = current + [value]
            try:
                # 先改内存再写盘，写盘失败回滚内存
                setattr(settings, raw_field, ",".join(new_list))
                _persist_whitelist_json(settings, settings.whitelist_path)
            except OSError as e:
                # 回滚内存，保证内存与磁盘一致
                setattr(settings, raw_field, ",".join(current))
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=persist_failed | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err("persist_failed", f"写入 whitelist.json 失败: {e}")

            elapsed = _elapsed_ms(start)
            logger.info(
                "tool={} | ok scope={} value={} | elapsed={}ms",
                tool_name,
                scope,
                value,
                elapsed,
            )
            return _ok(
                scope=scope,
                added=value,
                whitelist=new_list,
            )

    return add_whitelist_entry


def build_remove_whitelist_entry_tool(settings: Settings):
    """构建 remove_whitelist_entry 工具（白名单变更，需审批）。

    禁止删空：白名单至少保留 1 条（删空 = 所有容器/镜像操作全部被拒）
    持久化：同 add，内存 + whitelist.json 双写
    """

    @tool
    async def remove_whitelist_entry(scope: str, value: str) -> str:
        """从白名单删除条目。触发人工审批。禁止把白名单删空（至少保留 1 条）。

        Args:
            scope: 白名单类型，"container"（容器名）或 "image"（镜像前缀）
            value: 要删除的条目（必须已存在于白名单）
        """
        tool_name = "remove_whitelist_entry"
        start = time.perf_counter()
        logger.info(
            "tool={} | args=scope={} value={}", tool_name, scope, value
        )

        if scope not in _WHITELIST_SCOPES:
            return _err(
                "validation_error",
                f"scope 必须是 {'/'.join(_WHITELIST_SCOPES)} 之一",
                scope=scope,
            )
        if not value or not value.strip():
            return _err("validation_error", "value 不能为空")

        raw_field, list_attr, display = _WHITELIST_SCOPES[scope]
        async with _whitelist_lock:
            current: list[str] = getattr(settings, list_attr)
            if value not in current:
                elapsed = _elapsed_ms(start)
                logger.info(
                    "tool={} | not_found | elapsed={}ms", tool_name, elapsed
                )
                return _err(
                    "not_found",
                    f"{display} {value} 不在白名单内",
                    whitelist=current,
                )
            # 禁止删空：至少保留 1 条，否则容器/镜像操作全部被拒
            if len(current) <= 1:
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=cannot_remove_last | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err(
                    "cannot_remove_last",
                    "禁止删除白名单最后一条（删空将导致所有容器/镜像操作被拒绝）",
                    whitelist=current,
                )

            new_list = [item for item in current if item != value]
            try:
                # 先改内存再写盘，写盘失败回滚
                setattr(settings, raw_field, ",".join(new_list))
                _persist_whitelist_json(settings, settings.whitelist_path)
            except OSError as e:
                setattr(settings, raw_field, ",".join(current))
                elapsed = _elapsed_ms(start)
                logger.error(
                    "tool={} | error=persist_failed | elapsed={}ms",
                    tool_name,
                    elapsed,
                )
                return _err("persist_failed", f"写入 whitelist.json 失败: {e}")

            elapsed = _elapsed_ms(start)
            logger.info(
                "tool={} | ok scope={} value={} | elapsed={}ms",
                tool_name,
                scope,
                value,
                elapsed,
            )
            return _ok(
                scope=scope,
                removed=value,
                whitelist=new_list,
            )

    return remove_whitelist_entry


def build_tools(settings: Settings) -> list:
    """构建 15 个业务工具列表（8 个需审批的写操作 + 7 个只读/巡检/文件操作）。"""
    return [
        build_git_pull_code_tool(settings),
        build_docker_image_tool(settings),
        build_stop_container_tool(settings),
        build_remove_container_tool(settings),
        build_start_container_tool(settings),
        build_check_service_health_tool(settings),
        build_list_containers_tool(settings),
        build_list_images_tool(settings),
        build_check_dockerfile_tool(settings),
        build_list_workspace_files_tool(settings),
        build_read_workspace_file_tool(settings),
        build_write_workspace_file_tool(settings),
        build_delete_workspace_file_tool(settings),
        build_add_whitelist_entry_tool(settings),
        build_remove_whitelist_entry_tool(settings),
    ]


__all__ = [
    "build_tools",
    "build_git_pull_code_tool",
    "build_docker_image_tool",
    "build_stop_container_tool",
    "build_remove_container_tool",
    "build_start_container_tool",
    "build_check_service_health_tool",
    "build_list_containers_tool",
    "build_list_images_tool",
    "build_check_dockerfile_tool",
    "build_list_workspace_files_tool",
    "build_read_workspace_file_tool",
    "build_write_workspace_file_tool",
    "build_delete_workspace_file_tool",
    "build_add_whitelist_entry_tool",
    "build_remove_whitelist_entry_tool",
]
