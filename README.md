# Clienteling Workbench · 客户沟通工作台

面向客户顾问的 AI 微信回复助手，支持客户记忆、表达偏好和跟进管理。

## 演示

https://github.com/user-attachments/assets/abb49ac4-9096-4b49-bf79-831ae92ab51c

## 功能

- **客户记忆**：结合对话、客户资料与商品信息生成回复；记忆保留来源，支持人工修正。
- **表达校准**：将顾问选用、修改过的回复作为后续参考，也支持自定义表达要求和示例。
- **跟进判断**：新消息或约定时间触发处理，生成候选回复或建议暂缓，由顾问决定是否采用。

![客户工作台：左侧查看客户，中间延续对话，右侧选择或修改回复](docs/images/workbench.png)

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
