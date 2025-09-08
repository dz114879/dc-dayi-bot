# Discord 答疑机器人

这是一个专为 Discord 服务器设计的多功能AI机器人，旨在通过前沿的AI技术栈，为用户提供精准、高效的问答服务，并集成了一系列自动化管理工具。

## ✨ 核心特性

- **🧠 RAG (Retrieval-Augmented Generation)**: 结合了检索与生成技术，能从专门构建的知识库中提取精确信息，为用户提供有理有据的回答。
- **👁️ 多模态支持**: 能够理解和分析图片内容，实现图文结合的复杂问答。
- **⚙️ 模块化设计**: 基于 `cogs` 的模块化架构，易于扩展和维护。

---

## 🔧 安装与配置

1.  **克隆仓库**:
    ```bash
    git clone https://github.com/your-repo/your-bot.git
    cd your-bot
    ```

2.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **创建配置文件**:
    - 将 `.env.example` 复制为 `.env`。
    - 在 `.env` 文件中填入必要信息，包括 `DISCORD_BOT_TOKEN`, `OPENAI_API_KEY` 等。

4. **填入提示词**:
    - 在根目录新建prompt文件夹，在其中新建ALL.txt，这里的提示词将用作rag查询失败时的备用方案。
    - 在根目录新建rag_prompt文件夹，在其中新建ALL.txt，这里的提示词将被向量化并用作主要提示词。
      - 使用markdown格式编写此文档，用`===`分隔大段，`---`分隔小段以取得最佳效果（由rag_indexer.py决定）。

5.  **初始化RAG (首次运行)**:
   
    - 运行脚本以初始化 ChromaDB 向量数据库。
    ```bash
    python scripts/init_rag.py
    ```

6.  **运行机器人**:
    ```bash
    python bot.py
    ```

7. (可选)**配置反馈功能**:
    - 在根目录新建commit_prompt文件夹，其中放置commit_head.txt和commit_end.txt，分别作为提交反馈后LLM自动分析时接收到提示词的头部和尾部。
    - 在rag_prompt文件夹中新建commited.txt，留空即可。

## 邀请机器人

按照 Discord 开发者门户的标准流程，生成邀请链接并勾选以下权限：
- **Scopes**: `bot`, `applications.commands`
- **Bot Permissions**: `发送消息`, `在帖子中发送消息`, `嵌入链接`, `读取消息历史`, `附加文件`

---

## 📁 项目结构概览

```
.
├── cogs/               # 主要功能模块 (Cogs)
│   ├── agent.py        # Agent 智能体
│   ├── appdayi.py      # 应用开发答疑 (RAG)
│   ├── knownerdayi.py  # 知识库问答 (RAG)
│   ├── multimodal_embedding.py # 多模态嵌入处理
│   └── rag_processor.py # RAG 核心处理器
├── prompt/             # 知识库1
├── rag_prompt/             # 知识库2
├── rag_data/           # RAG 数据 (包括 ChromaDB)
├── scripts/            # 测试与工具脚本
├── bot.py              # 机器人主入口
└── requirements.txt    # Python 依赖