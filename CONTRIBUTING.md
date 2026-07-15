# 17deg Atlas 贡献指南

感谢你对 17deg Atlas 项目的关注！我们欢迎任何形式的贡献。

## 开始贡献

### 环境要求
- Python >= 3.10

### 快速体验
```bash
python scripts/17deg-atlas.py --help
```

### 运行测试
```bash
python -m unittest discover -s modules/knowledge/tests -v
```

## 贡献流程

### 1. 讨论较大变更
对于重大功能、架构调整或不兼容的变更，请先通过 GitHub Issue 讨论获得确认后再开始实施。

### 2. 基本工作流程
1. **Fork** 本仓库到你的 GitHub 账户
2. **创建分支**：`git checkout -b feature/your-feature-name`
3. **提交变更**：`git commit -m "描述你的变更"`
4. **推送分支**：`git push origin feature/your-feature-name`
5. **创建 Pull Request**：在 GitHub 上提交 PR

### 3. 代码检查
提交前请运行格式检查：
```bash
git diff --check
```

确保没有尾随空格或其他格式问题。

## 提交规范

### 不得提交的内容
- 密钥、凭证等敏感信息
- 个人知识库内容
- 测试解密产物

### 文字规范
- 公开文字保持面向用户，使用清晰易懂的语言
- 只描述已经验证且用户需要了解的功能

## 许可证
提交贡献即表示你同意将代码以 MIT 许可证发布。

---

再次感谢你的贡献！
