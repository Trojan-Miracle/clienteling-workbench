# Clienteling Workbench · 客户沟通工作台

帮助客户顾问写微信回复：说得自然，也更像自己会说的话。

## 演示

https://github.com/user-attachments/assets/abb49ac4-9096-4b49-bf79-831ae92ab51c

## 更自然，也更像你

客户说了什么、上次聊到哪里、这次要解决什么，放在一起考虑。回复的长短、称呼和收尾随对话决定，减少套话和无关展开。

你采用过的回复会连同当时情境保存，后续起草时作为表达参考。也可以直接写下自己的要求，整理喜欢的例子，不用每次重新交代说话习惯。

三条回复可以选择、直接编辑，或按一句修改意见重写。保存的要求和例子都能查看、修改和停用。

![客户工作台：左侧查看客户，中间延续对话，右侧选择或修改回复](docs/images/workbench.png)

客户对话、购买记录、服务进展和记忆在同一处管理。新消息与约定时间触发复查，适合联系时准备草稿，需要等待时暂缓。

## 系统结构

![客户关系与品牌资料进入上下文，经过生成和审查，由顾问选择，结果留作后续参考](docs/architecture.svg)

## 快速开始

建议使用 Python 3.11–3.13。以下命令适用于 Linux / macOS 终端，Windows 可使用 WSL；已在 Linux / Python 3.13 验证。

```bash
git clone https://github.com/Trojan-Miracle/clienteling-workbench.git
cd clienteling-workbench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-memory.txt
```

将 [.env.example](.env.example) 复制为 `.env`，填写自己的 `DEEPSEEK_API_KEY`。已有 `.env` 不要覆盖，其余配置可保持默认。

首次运行，准备演示数据并下载中文检索模型：

```bash
python scripts/setup_demo.py
python scripts/setup_memory_retrieval.py
```

将 `XXXX` 换成你选择的可用端口后启动：

```bash
python scripts/serve_review.py --port XXXX --retrieval hybrid
```

浏览器打开 `http://127.0.0.1:XXXX`，使用相同端口。远程服务器需先在 VS Code「端口」面板转发所选端口，再打开显示的本地地址。

演示包含8位客户和已生成的建议，查看已有内容不调用模型；新消息与改写需要可用的 API 密钥和网络连接。演示时间设在11月7日，右上角菜单可调整。

工作数据保存在 `artifacts/`，不会进入 Git。`setup_demo.py` 只在没有工作数据库时初始化，不会覆盖已有记录。

## 代码与测试

| 目录 | 内容 |
| --- | --- |
| `atelier/` | 客户关系、上下文检索、起草、审查与跟进 |
| `web/v6/` | 工作台界面 |
| `prompts/v6/` | 实际使用的提示词 |
| `data/v6/` | 品牌、商品、活动与演示客户设定 |
| `tests/` | 记忆、改写、跟进、任务恢复等自动测试 |

```bash
python -m unittest discover -s tests -v
```

演示采用虚构皮具品牌 MAISON VIREL，客户和商品均为演示设定。应用面向本地单顾问使用，由顾问核对并自行发送，不接入或自动发送微信。
