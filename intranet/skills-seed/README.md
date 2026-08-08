# 技能库初始源（手工导入）

本目录已预置示例技能：

- `code-review-checklist/` — 代码评审清单
- `release-notes/` — 发版说明起草

把要共享的技能目录放到这里，再复制到运行时持久化目录：

```bash
# 在 zcode-mirror/intranet 下
mkdir -p config/skills
cp -R skills-seed/<skill-name> config/skills/<skill-name>
# 或一次性同步全部示例：
cp -R skills-seed/code-review-checklist skills-seed/release-notes config/skills/
```

每个技能目录必须包含 `SKILL.md`，例如：

```text
config/skills/code-review-checklist/SKILL.md
```

`config/skills/` 挂载为容器内 `/config/skills/`，**不会**被 `.zdoc` 内容导入覆盖。

也可以在管理后台 **技能库** Tab 上传 zip / 粘贴创建。局域网用户访问：

```text
http://<内网IP>:8090/skills
```
