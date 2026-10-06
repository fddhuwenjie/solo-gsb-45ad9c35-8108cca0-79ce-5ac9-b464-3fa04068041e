# 🎲 可公开核验的现场抽签（Flask + SQLite + 原生 JavaScript）

一个用于现场抽奖/抽签的小型 Web 系统。核心是**承诺—封存—揭晓（commit–seal–reveal）**流程：
种子在浏览器本地生成，服务器事先只拿到 SHA-256 承诺；名单封存后不可改；
揭晓时任何人都能用事先留存的承诺、名单摘要和**公开的确定性散列规则**独立复算结果，
而不是只能相信服务器保存的中奖列表。

无需外部账号、无需付费服务、无需联网 CDN（SHA-256 为自带纯 JS 实现，HTTP 现场环境可用）。

---

## 目录

- [一、信任模型与规则](#一信任模型与规则)
- [二、一条命令启动（Docker Compose）](#二一条命令启动docker-compose)
- [三、访问地址与端口](#三访问地址与端口)
- [四、完整操作示例（网页界面）](#四完整操作示例网页界面)
- [五、完整操作示例（命令行 / API）](#五完整操作示例命令行--api)
- [六、导出核验包与独立核验](#六导出核验包与独立核验)
- [七、不用 Docker 的本地运行方式](#七不用-docker-的本地运行方式)
- [八、项目结构与接口说明](#八项目结构与接口说明)

---

## 一、信任模型与规则

规则标识：`sha256-argmax-v1`，完整文字见 [`RULES.md`](RULES.md)。摘要：

1. **创建轮次（承诺）**：浏览器用密码学随机数生成 32 字节（256 位）种子 `seed`（64 位十六进制），
   只把承诺 `SHA256("COMMIT:"+seed)` 和中奖名额提交给服务器。页面提示保存种子并展示承诺供现场事先留存。
2. **登记名单**：按唯一编号登记，封存前可增删、整体替换；服务器拒绝重复编号。
3. **封存名单**：人数必须 ≥ 名额。封存时把编号按 **UTF-8 字节序**排序得到规范快照，
   连同 `SHA256(快照)` 摘要一次性保存并展示，**此后服务器拒绝任何名单修改**。
4. **揭晓种子**：
   - 提交的种子若 `SHA256("COMMIT:"+seed)` 与承诺不符 → **轮次保持封存状态**，只记录失败次数；
   - 相符 → 按公开规则选出**不重复**的中奖编号，结果与状态在同一事务一次保存；
   - 重复提交种子只返回既有结果，**不会重新抽奖**。
5. **抽奖规则**：第 `i` 轮（`i=0,1,…`）对每个未中奖编号 `id` 计算
   `score = int_be(SHA256("sha256-argmax-v1|"+seed+"|"+id+"|"+i))`，取分数最大者，
   平局取字节序靠前者。全程不取模，没有取模偏置。

> 安全性前提：操作者在揭晓前对种子保密；承诺与名单摘要由现场监督方事先独立留存。
> 服务器只是“承诺托管板 + 防篡改的名单快照”，种子从不被服务器提前掌握。

---

## 二、一条命令启动（Docker Compose）

前置：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up -d --build
```

首次会构建镜像并启动；数据保存在命名卷 `lottery-data`（容器内 `/data/lottery.db`），
容器重建后数据仍在。停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

---

## 三、访问地址与端口

| 用途 | 地址 |
| --- | --- |
| 新建轮次 / 轮次列表 | <http://localhost:8000/> |
| 轮次详情（登记/封存/揭晓/导出） | `http://localhost:8000/rounds/<轮次编号>` |
| 浏览器内核验工具 | <http://localhost:8000/verify> |
| 规则文档 | <http://localhost:8000/RULES.md> |

服务监听容器内 **8000** 端口，经 `docker-compose.yml` 映射到宿主机 **8000**。
局域网内其他人访问时，把 `localhost` 换成运行机器的局域网 IP，例如
`http://192.168.1.50:8000/`（需放行防火墙 8000 端口）。
想改宿主机端口，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧数字即可。

---

## 四、完整操作示例（网页界面）

### 步骤 1：创建轮次（种子不出本机）

1. 打开 <http://localhost:8000/>，填写“轮次名称”和“中奖名额”（例：`年会抽奖`、`3`）。
2. 点击 **“生成种子”**。页面上会出现：
   - 橙色虚线框里的**秘密种子**（64 位十六进制）；
   - 下方的**承诺**（`SHA256("COMMIT:"+种子)`）。
3. 点击 **“复制种子”** 或 **“下载 seed.txt”** 妥善保存（揭晓前保密，丢失则无法揭晓）；
   把**承诺**投影/拍照/抄写给现场人员事先留存。
4. 勾选“我已保存种子并留存承诺”，点击 **“仅提交承诺，创建轮次”**，自动进入轮次页。

### 步骤 2：登记参与者（封存前可改）

- 在“逐个添加”输入编号回车；或在“批量导入”中每行一个编号，点“替换为以上名单”。
- 编号自动去除首尾空白；重复编号会被拒绝。封存前可随时删除或整体替换。
- 页面底部可预览按 UTF-8 字节序排序的规范名单及本地试算摘要。

### 步骤 3：封存名单

- 当人数 ≥ 名额时 **“封存名单”** 按钮才可点击。确认后：
- 页面展示**名单摘要**（64 位十六进制）、封存时间和规范名单快照；
- 名单区消失，服务器此后拒绝添加、删除、替换（尝试修改会返回 409“名单已封存”）。
- 请现场人员核对并留存摘要。

### 步骤 4：揭晓

1. 粘贴事先保存的种子，点击 **“校验承诺并揭晓”**。
2. 种子错误 → 页面提示“种子与事先留存的承诺不匹配，轮次保持封存状态”，
   可修正后重试（失败计数仅作记录，不影响正确揭晓）。
3. 种子正确 → 页面按产生顺序展示中奖编号、揭晓种子与时间；结果永久固定，
   再次提交任何种子都不会重抽。

### 步骤 5：导出与核验

点击 **“下载 ZIP 核验包”**（或仅下载 `package.json`），交给任意监督方核验。
可在 <http://localhost:8000/verify> 上传 `package.json` 在线核验，
或用包内 `verify.py` 命令行核验（见下一节）。

---

## 五、完整操作示例（命令行 / API）

下面用 `curl` 完整走一遍。为方便复现，使用固定演示种子
`0123456789abcdef` 重复 4 次（真实抽签请用浏览器生成的随机种子！）：

```bash
SEED=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
COMMIT=$(python3 -c 'import hashlib,os;print(hashlib.sha256(("COMMIT:"+os.environ["SEED"]).encode()).hexdigest())')
echo "$COMMIT"
# be98cadeb4969ad6bb53c25aa2217fa8b8bf27174e9d7d7a61f963fd6b8cc167
```

```bash
# 1) 创建轮次（只提交承诺）
curl -s -X POST localhost:8000/api/rounds -H 'Content-Type: application/json' \
  -d "{\"title\":\"命令行演示\",\"prizeCount\":3,\"commitment\":\"$COMMIT\"}"
# 记下返回的 "id"，例如 9f2c...
RID=<上一步返回的轮次编号>
```

```bash
# 2) 登记名单（封存前可反复替换）
curl -s -X PUT localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' \
  -d '{"participants":["A001","A002","A003","A004","A005"]}'

# 重复编号会被拒绝：
curl -s -X PUT localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' \
  -d '{"participants":["A001","A001"]}'
# {"ok":false,"error":"编号重复：A001"}
```

```bash
# 3) 封存（人数不足会被拒绝；封存后改名单返回 409）
curl -s -X POST localhost:8000/api/rounds/$RID/seal
# 返回 status=sealed、snapshot、listDigest：
#   listDigest = f798b141dcaca51093500b25f0c442bc95f72343dc84cf42e26bdc629aa2c648

curl -s -X POST localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' -d '{"id":"HACKER"}'
# {"ok":false,"error":"名单已封存，此后拒绝任何修改"}
```

```bash
# 4a) 错误种子：保持封存
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d '{"seed":"ff00...（64位）"}'
# {"ok":false,"commitMismatch":true,...,"round":{"status":"sealed",...}}

# 4b) 正确种子：揭晓
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d "{\"seed\":\"$SEED\"}"
# winners = ["A003","A005","A001"] —— 任何人按规则都能算出同样结果

# 4c) 再次提交：不重抽
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d "{\"seed\":\"$SEED\"}"
# {"ok":true,"alreadyRevealed":true,"message":"该轮次已揭晓，结果固定，重复提交不会重抽",...}
```

```bash
# 5) 导出核验包
curl -s -OJ localhost:8000/api/rounds/$RID/package.zip
unzip lottery-$RID-package.zip
# 生成目录 lottery-$RID/：package.json  list.txt  RULES.md  verify.py
```

---

## 六、导出核验包与独立核验

核验包（zip）内容：

| 文件 | 内容 |
| --- | --- |
| `package.json` | 轮次元数据、承诺、种子（未揭晓时为 `null`）、规范名单、名单摘要、规则标识、中奖结果 |
| `list.txt` | 规范名单（每行一个编号） |
| `RULES.md` | 抽奖规则全文 |
| `verify.py` | 独立核验脚本，**仅依赖 Python 3 标准库** |

命令行核验（Python 3.8+，无需安装任何东西）：

```bash
python3 verify.py package.json
```

脚本**不相信**包里的 `winners` 字段，而是：

1. 用种子重算 `SHA256("COMMIT:"+种子)` 与承诺比对；
2. 对名单重排、查重并重算规范快照摘要，与包内摘要比对；
3. 检查中奖编号数量、唯一性、是否都在名单内；
4. **从种子和名单按 `sha256-argmax-v1` 重新抽奖**，与包内结果逐项比对；
5. 任何不符逐条打印 `[不符]`，进程以退出码 `1` 结束；全部通过输出“全部核验通过”，退出码 `0`。

结合事先留存的承诺和名单摘要（推荐的真正“外部”核验）：

```bash
python3 verify.py package.json \
  --commitment be98cadeb4969ad6bb53c25aa2217fa8b8bf27174e9d7d7a61f963fd6b8cc167 \
  --list-digest f798b141dcaca51093500b25f0c442bc95f72343dc84cf42e26bdc629aa2c648
```

zip 也可直接传入：`python3 verify.py lottery-<编号>-package.zip`。

对上面命令行演示的固定例子，独立重算结果必然是：

```text
第 1 位 A003
第 2 位 A005
第 3 位 A001
```

你可以自行做篡改实验：改 `package.json` 里的一个中奖编号、调换名单顺序、换一个种子……
核验器都会指出具体不符项。

> 封存后、揭晓前也可以导出核验包：此时包内种子与结果为空，核验器核对承诺与名单摘要，
> 并提示“处于封存（未揭晓）状态”。

---

## 七、不用 Docker 的本地运行方式

需要 Python 3.10+：

```bash
pip install -r requirements.txt
# 开发调试
python wsgi.py                 # http://localhost:8000

# 或用 gunicorn（Linux）
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 wsgi:app
```

数据库默认是当前目录下的 `lottery.db`，可用环境变量 `LOTTERY_DB` 修改位置。
gunicorn 建议保持单 worker（SQLite 写串行化即可；多线程没有问题）。

---

## 八、项目结构与接口说明

```
.
├── app/
│   ├── __init__.py        # Flask 应用工厂（静态目录指向仓库根 static/）
│   ├── db.py              # SQLite 连接、建表、状态机注释
│   ├── rules.py           # 公开规则的 Python 实现（唯一事实来源之一）
│   ├── routes.py          # 页面路由、JSON API、核验包 zip/json 导出
│   └── templates/         # index / round / verify 三个页面（Jinja2）
├── static/
│   ├── css/style.css
│   └── js/
│       ├── sha256.js      # 纯 JS SHA-256（无需安全上下文/CDN）
│       ├── rules.js       # 与 Python 等价的浏览器规则实现（BigInt）
│       ├── common.js      # fetch/转义/复制/下载工具
│       ├── index.js       # 生成种子、只交承诺、轮次列表
│       ├── round.js       # 名单增改、封存、揭晓、导出
│       └── verify.js      # 浏览器内核验（独立重算）
├── verify/verify.py       # 独立核验脚本（仅 Python 标准库）
├── RULES.md               # 规则全文
├── wsgi.py                # gunicorn wsgi:app
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

状态机：`open`（可改名单）→ `sealed`（快照+摘要固定，等种子）→ `revealed`（结果永久固定）。

主要 API：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/rounds` | 创建轮次：`{title, prizeCount, commitment}` |
| `GET  /api/rounds` | 轮次列表 |
| `GET  /api/rounds/<id>` | 轮次详情（含名单/快照/结果） |
| `POST /api/rounds/<id>/participants` | 添加编号（仅 open） |
| `PUT  /api/rounds/<id>/participants` | 整体替换名单（仅 open） |
| `DELETE /api/rounds/<id>/participants/<编号>` | 删除编号（仅 open） |
| `POST /api/rounds/<id>/seal` | 封存（人数 ≥ 名额；幂等） |
| `POST /api/rounds/<id>/reveal` | 提交种子；不匹配保持封存，重复揭晓不重抽 |
| `GET  /api/rounds/<id>/package.json` | 导出 JSON 核验包 |
| `GET  /api/rounds/<id>/package.zip` | 导出 ZIP 核验包（含规则与核验器） |
