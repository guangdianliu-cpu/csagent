# Skill 包编写指南

## 目录结构

每个 Skill 是一个独立目录，放在任意已配置的 Skill 根目录下：

```text
skills/
└── example-skill/          # 目录名 = Skill name（kebab-case）
    ├── SKILL.md            # 必须：元数据 + 操作指南
    ├── tools.yaml          # 可选：声明原生工具（见下文「声明原生工具」）
    ├── scripts/            # 可选：脚本资源
    │   └── generate.py
    ├── references/         # 可选：参考文档
    │   └── template.md
    └── assets/             # 可选：静态资源
        └── logo.png
```

## SKILL.md 格式

```markdown
---
name: example-skill
description: 示例 Skill 的用途说明，描述需足够具体以便模型判断何时加载。
version: 1.2.0
tags: [example]
allowed-tools: [read_file, write_file, run_script]
enabled: true
---

# 示例 Skill 操作指南

（此处为操作正文：步骤、约束、示例等。模型通过 load_skill 获取这部分内容。）
```

### Frontmatter 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | Skill 唯一名，必须与目录名一致，kebab-case |
| `description` | 是 | 简述用途（>= 20 字符） |
| `version` | 否 | 语义化版本，默认 `1.0.0` |
| `tags` | 否 | 标签列表，用于搜索过滤 |
| `allowed-tools` | 否 | 工具白名单；为空不限制。**必须列全正文里让模型调用的每个工具**（含 `read_file` 读 references、`write_script` 写二次加工脚本等）——开启强制模式后，没列的工具会被拒绝 |
| `enabled` | 否 | 全局禁用开关，默认 `true`。**某个部署形态能看到哪些 Skill 由 `configs/profiles/<形态>/profile.yaml` 的 `skills` 白名单决定**；形态写了白名单时此字段不再参与判断，只有形态没写白名单时才生效。切换形态不要改这里 |
| `python-path` | 否 | Skill 内的目录列表（如 `[scripts]`）。run_script 把它们放进子进程 PYTHONPATH，模型现写的会话脚本可以直接 `import` 里面的模块，不用自己拼 `sys.path`。多个 Skill 有同名模块时按 Skill 名排序先到先得，模块名要起得独特 |

### 校验规则

- 缺 `name` 或 `description` → 拒绝加载
- `name` 与目录名不一致 → 拒绝加载
- `description` < 20 字符 → 拒绝加载
- 正文 > 20000 字符 → 告警（建议拆分到 `references/`）

## 多根配置

在 `configs/tool_packs.yaml` 的 `skills.config.roots` 中配置：

```yaml
skills:
  pack_class: csagent.tools.packs.skill_pack.SkillToolPack
  config:
    roots:
      - skills                  # 仓库内置（低优先级）
      - ~/.csagent/skills       # 用户级
      - .csagent/skills         # 项目级（高优先级）
```

同名 Skill 后者覆盖前者（高优先级胜出）。

## 工具交互

| 工具 | 用途 |
|------|------|
| `list_skills` | 列出可用 Skill，支持模糊过滤 |
| `load_skill` | 加载 Skill 正文 + 资源清单（正文里已写了路径的文件不再重复列） |

加载后，Skill 携带的资源可通过路径前缀访问：

```text
read_file(path="<skill:example-skill>/references/template.md")
run_script(script="<skill:example-skill>/scripts/generate.py", args=[...])
```

形态里几乎每问都要用的 Skill，可以在 `configs/profiles/<形态>/profile.yaml` 的 `resident_skills` 里列出：
正文每轮直接放在系统提示词末尾，模型不用先 `load_skill`。正文仍按 12000 字截断，常驻的 Skill 更要写短。

## 声明原生工具（tools.yaml）

一次调用就能完成的业务查询，别让模型经 `run_script` 把 JSON 塞进命令行字符串（两层转义、没有
schema 校验，错了要多跑一轮才知道）。在 Skill 根目录写 `tools.yaml`，启动时内核把每一项注册成
原生 function calling 工具：

```yaml
tools:
  - name: example_lookup            # 小写下划线，全局唯一
    script: scripts/query.py        # 必须在 scripts/ 下
    argv: [--action, lookup, --payload, "-"]   # 固定参数；工具参数 JSON 经 stdin 传入
    description: >-
      做什么、做不到什么、做不到时该用什么
    parameters:                     # JSON Schema，type 必须是 object
      type: object
      properties:
        code: {type: string, description: 6 位代码}
      required: [code]
      additionalProperties: false
```

脚本侧的契约：
- 参数 JSON 从 stdin 读（约定 `--payload -`），不要再从命令行解析 JSON
- 成功：结果写 stdout，退出码 0
- 失败：stdout 写 `{"ok": false, "error": {"code": "...", "message": "..."}}`，退出码非 0。内核据退出码
  把这次调用记为失败（审计 `status=error`），并把这段 JSON 原样回给模型；`message` 要写清怎么改
- 环境变量、超时、输出上限与 run_script 跑 Skill 脚本一致（含 `env_extra`）

另外两件事：新工具要在 `configs/policy.yaml` 的 `tool_risks` 里配风险等级（否则启动时报
`tool_policy_unclassified_tools`）；`tools.yaml` 改动要重启进程才生效。写错的单个工具会被跳过并报错，
不影响 Skill 其余部分。

## 权限控制

当 `configs/tool_packs.yaml` 的 `skills.config.enforce_allowed_tools: true` 时：
- `load_skill` 会将该 Skill 的 `allowed-tools` 设为会话级硬约束；Skill 自己在 `tools.yaml` 里声明的工具自动算在内
- 超出白名单的工具调用会被 ToolRegistry 拦截
- `list_skills` / `load_skill` 等管理工具始终放行

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 列出已加载 Skill |
| GET | `/api/skills/{name}` | 获取 Skill 详情 |
| POST | `/api/skills/reload` | 触发热重载 |
