"""部署 Agent 配置。

模仿参考项目 ontology_agent.settings 的写法：
- 用 pydantic-settings BaseSettings + .env 加载
- 敏感字段用 SecretStr，URL 字段用 AnyHttpUrl
- settings_customise_sources 让本地 .env 优先于继承的 shell 环境变量
  （开发期：.env 里的值覆盖 shell env；无 .env 时退回 shell env + 默认值）
- get_settings 用 lru_cache 单例
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# 项目根目录（backend/），.env 放在这里
# parents[0]=deploy_agent, parents[1]=src, parents[2]=backend
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """部署 Agent 运行所需配置。

    所有字段通过 validation_alias 与 .env / 环境变量名对齐（大写形式）。
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        # 允许直接构造时用字段名传参（默认只接受 validation_alias）。
        # 测试用 Settings(container_names_raw=..., workspaces_raw=...) 构造时必须开此项，
        # 否则字段名 kwarg 被忽略，值回退到 .env（多值测试场景会失效）。
        # 生产 get_settings() 不传参，从 .env 加载，不受影响。
        populate_by_name=True,
    )

    # --- LLM / OpenAI ---
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: AnyHttpUrl | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.0, validation_alias="OPENAI_TEMPERATURE")

    # --- 代码仓库 ---
    # 仓库地址和分支由用户在对话中指定，不做白名单校验
    # 鉴权仍用 GITLAB_USER + GITLAB_TOKEN 注入到用户传入的 repo_url

    # --- 容器 / 镜像白名单 ---
    # 容器名白名单（多值，逗号分隔），stop/start_container 的 container_name 必须在此列表内
    # 初始值来自 .env 的 CONTAINER_NAMES；一旦 whitelist.json 存在，以 JSON 为准（对话中增删的持久化结果）
    container_names_raw: str = Field(default="", validation_alias="CONTAINER_NAMES")
    # 镜像名前缀白名单（多值，逗号分隔），build_docker_image / start_container 的 image 必须命中其中之一
    # 初始值来自 .env 的 IMAGE_PREFIX；一旦 whitelist.json 存在，以 JSON 为准
    # 环境变量名仍用 IMAGE_PREFIX（保持向后兼容），但语义已改为多值列表
    image_prefixes_raw: str = Field(default="", validation_alias="IMAGE_PREFIX")
    # workspace 白名单（多值，逗号分隔），git_pull_code 的 workspace 必须在此列表内
    # workspace 不支持对话中增删，只走 .env
    workspaces_raw: str = Field(default="/data/deploy/workspace", validation_alias="WORKSPACES")
    # 白名单持久化 JSON 文件路径（容器/镜像白名单的运行时存储）
    # 空值时用默认路径 backend/whitelist.json；测试时传 tmp 路径隔离
    whitelist_file: str = Field(default="", validation_alias="WHITELIST_FILE")

    # --- 私有仓库拉取令牌（可选）---
    gitlab_token: SecretStr | None = Field(default=None, validation_alias="GITLAB_TOKEN")
    # GitLab 账号（与 token 配合用于 git clone 鉴权，可选）
    gitlab_user: str | None = Field(default=None, validation_alias="GITLAB_USER")

    # --- SSH 目标服务器（git/docker 在此执行）---
    server_host: str | None = Field(default=None, validation_alias="SERVER_HOST")
    server_port: int = Field(default=22, validation_alias="SERVER_PORT")
    server_user: str | None = Field(default=None, validation_alias="SERVER_USER")
    server_password: SecretStr | None = Field(default=None, validation_alias="SERVER_PASSWORD")

    # --- 健康检查 / 镜像 tag 格式 ---
    health_url: str | None = Field(default=None, validation_alias="HEALTH_URL")
    image_tag_format: str = Field(default="%Y%m%d-%H%M%S", validation_alias="IMAGE_TAG_FORMAT")

    # --- Docker 主机（空=本机 docker daemon）---
    docker_host: str | None = Field(default=None, validation_alias="DOCKER_HOST")

    # --- 必须人工审批的工具 ---
    # 默认 stop/start container 需要审批，对应需求文档第七章
    approval_required_tools_raw: str = Field(
        default="stop_container,start_container",
        validation_alias="APPROVAL_REQUIRED_TOOLS",
    )

    # --- 服务监听 ---
    host: str = Field(default="127.0.0.1", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")

    # --- 日志 ---
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    # 相对路径基于项目根目录（backend/）解析
    log_dir: str = Field(default="logs", validation_alias="LOG_DIR")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 本地 .env 存在时优先于继承的 shell env（开发者笔记本友好）
        # 无 .env 时退回 shell env + 代码默认值
        if _ENV_FILE.is_file():
            return init_settings, dotenv_settings, env_settings, file_secret_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator(
        "openai_api_key",
        "openai_base_url",
        "gitlab_token",
        "gitlab_user",
        "server_host",
        "server_user",
        "server_password",
        "health_url",
        "docker_host",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        # .env 里留空值时统一转 None，避免后续 AnyHttpUrl 等校验失败
        # 注意：container_names_raw / workspaces_raw / image_prefixes_raw 是 str 类型，
        # 空串是合法值（表示白名单为空），不在此列
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    def model_post_init(self, __context: Any) -> None:
        """加载 whitelist.json（若存在），覆盖容器/镜像白名单的 .env 初始值。

        语义：JSON 是对话中增删白名单的持久化结果，优先于 .env。
        一旦 JSON 存在，改 .env 的 CONTAINER_NAMES/IMAGE_PREFIX 不再生效
        （要回到 .env 管理，删除 whitelist.json 即可）。
        JSON 损坏时静默回退 .env 值，不阻断启动。
        """
        path = self.whitelist_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        containers = data.get("container_names")
        if isinstance(containers, list) and containers:
            self.container_names_raw = ",".join(str(c) for c in containers)
        prefixes = data.get("image_prefixes")
        if isinstance(prefixes, list) and prefixes:
            self.image_prefixes_raw = ",".join(str(p) for p in prefixes)

    @property
    def whitelist_path(self) -> Path:
        """白名单 JSON 文件路径：WHITELIST_FILE 配置优先，默认 backend/whitelist.json。"""
        if self.whitelist_file.strip():
            return Path(self.whitelist_file.strip())
        return _PROJECT_ROOT / "whitelist.json"

    @property
    def container_names(self) -> list[str]:
        """容器名白名单：逗号分隔解析成 list。

        日常扩展白名单只需改 .env 的 CONTAINER_NAMES 后重启，无需动代码。
        """
        if not self.container_names_raw.strip():
            return []
        return [c.strip() for c in self.container_names_raw.split(",") if c.strip()]

    @property
    def image_prefixes(self) -> list[str]:
        """镜像名前缀白名单：逗号分隔解析成 list。

        日常扩展白名单只需改 .env 的 IMAGE_PREFIX 后重启，无需动代码。
        例如支持 ontology/a 与 infra/b 两个不相关前缀：
            IMAGE_PREFIX=ontology/a,infra/b
        """
        if not self.image_prefixes_raw.strip():
            return []
        return [p.strip() for p in self.image_prefixes_raw.split(",") if p.strip()]

    @property
    def workspaces(self) -> list[str]:
        """workspace 白名单：逗号分隔解析成 list。

        日常扩展白名单只需改 .env 的 WORKSPACES 后重启，无需动代码。
        """
        if not self.workspaces_raw.strip():
            return []
        return [w.strip() for w in self.workspaces_raw.split(",") if w.strip()]

    def approval_required_tools(self) -> list[str]:
        """解析 APPROVAL_REQUIRED_TOOLS，支持逗号分隔或 JSON 数组。"""
        value = self.approval_required_tools_raw
        if value is None:
            return []
        stripped = value.strip()
        if not stripped:
            return []
        # JSON 数组形式：["stop_container", "start_container"]
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("APPROVAL_REQUIRED_TOOLS 必须是 JSON 数组或逗号分隔列表。")
            return [str(item).strip() for item in parsed if str(item).strip()]
        # 逗号分隔形式：stop_container,start_container
        return [item.strip() for item in stripped.split(",") if item.strip()]

    def approval_tool_names(self) -> list[str]:
        """approval_required_tools 的别名，供 middleware 使用。"""
        return self.approval_required_tools()

    @property
    def model_ready(self) -> bool:
        """LLM 是否就绪（至少需要 API Key）。"""
        return self.openai_api_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例获取 Settings。"""
    return Settings()
