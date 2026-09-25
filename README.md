# AIS_streaming_testing_platform · EnvShip 在线预测平台 Demo

按 [`docs/roadmap.md`](docs/roadmap.md)（AIS 在线预测平台 · 技术路线图）**推荐的技术路线**实现的可运行竖切片（vertical slice）：
共享 AIS 流管道 → 预测器以插件容器 shadow 并行 → reconcile 在真值到达后统一打分 → live leaderboard + 地图界面 + 带真值的 parquet 数据集。

仓库自带 **约 40 分钟真实芬兰 AIS 数据（723 艘船、8.3 万条位置报文）**（Digitraffic，CC BY 4.0）作为 replay fixture，默认 20× 回放，**启动后约 1 分钟就能在地图上看到“预测 vs 真实”的打分结果**；也可以一键切换到 Digitraffic 实时 MQTT 流。

```mermaid
flowchart LR
  FI[Digitraffic<br/>MQTT push] --> ING[ingest-fi<br/>去重/黑名单/归档]
  RP[replay<br/>parquet N× 回放] --> BUS
  ING --> BUS[(Redis Streams)]
  BUS --> TRK[tracker + features<br/>20 s 因果网格 / 三分法 eligibility<br/>byte-parity 启动门]
  TRK -- windows.eligible --> BUS
  BUS --> P1[predictor: cv]
  BUS --> P2[predictor: kalman]
  BUS --> P3[predictor: imm]
  BUS --> P4[predictor: example<br/>SDK 30 行示例]
  P1 & P2 & P3 & P4 -- predictions.* --> BUS
  TRK -- truth.realized<br/>(10 min 后) --> BUS
  BUS --> REC[reconcile<br/>唯一裁判：served/best-of-K ADE、FDE、miss]
  REC --> PQ[(parquet zstd<br/>Hive 分区)]
  PQ --> JOBS[jobs · APScheduler<br/>DuckDB 物化 leaderboard/SLA]
  PQ --> API[FastAPI + SSE<br/>DuckDB 只读查询]
  BUS --> API
  API --> WEB[React + MapLibre + deck.gl + ECharts<br/>经 Caddy 提供]
```

## 快速开始

### 方式 A：本机一条命令（无需 Docker）

需要 Python 3.11、[uv](https://docs.astral.sh/uv/)、Node 20+、`redis-server`（没有在跑的话会自动拉起）。

```bash
uv sync                                  # Python workspace（contracts / sdk / core / predictors）
(cd web && npm ci && npm run build)      # 前端静态包，由 api 直接托管
uv run envship dev --fresh               # replay 20×；打开 http://localhost:8000
uv run envship dev --live                # 改用 Digitraffic 实时 MQTT 流
```

`envship dev` 会启动 tracker、reconcile、jobs、api、4 个预测器和数据源（replay 或 ingest-fi），日志带颜色前缀，Ctrl-C 全部停止。

### 方式 B：docker compose（路线图 §3.5 的部署形态）

```bash
docker compose --profile replay up --build        # 打开 http://localhost:8080
docker compose --profile live up --build          # 实时 Digitraffic
docker compose --profile replay --profile ops up  # 另加 Prometheus :9090 / Grafana :3000
REPLAY_SPEED=50 docker compose --profile replay up
```

Caddy 负责静态前端和反向代理（设置 `SITE_ADDRESS=your.domain` 即自动 TLS）；预测器容器按路线图限 2 CPU / 4 GB。

## 怎么观察效果

| 页面 | 看什么 |
| --- | --- |
| **Live map** | 灰点是所有在播船只，深色点是已进入 eligible 窗口的船。左侧 *Recent verdicts* 每行是一个 10 分钟未来已经发生的窗口，列出各预测器的 served ADE（加粗 = 本窗口最佳）。**点一行或点任意船**：地图显示 10 分钟历史（灰）、各预测器候选（彩色，粗线 = served，细线 = 其余 K 个候选）、真实轨迹（黑/白粗线），右侧表格给出 served ADE / best-of-K / FDE。窗口未到期时显示 “truth due in X event-min”。 |
| **Leaderboard** | 统一裁判下的榜单：served ADE + bootstrap 95% CI、best-of-K、FDE、coverage（5 s 内作答比例）、compute p50；可按时间窗和分层（benchmark-comparable / 直航 / 转向 / 速度档）切换；下方是误差随预测时域的曲线和随时间的走势。 |
| **SLA & health** | 三个时钟（anchor / enqueue / issue）得出的队列延迟 p50/p90/p99、5 s 预算达标率；每个服务的心跳、积压、parity 状态和计数器（eligible / gap / warmup / stationary 等）。 |
| **Predictors & data** | 预测器注册表（存活状态、版本、K）；DuckDB 只读 SQL 控制台，内置示例查询（按场景的榜单、IMM 胜过 CV 的窗口、eligibility 漏斗……）。 |

replay 模式下事件时间是 20× 的：真值在锚点后 10 分钟事件时间（≈ 33 秒墙钟）到达，所以打分几乎是实时滚动出来的。

## 与路线图的对应

| 路线图推荐 | 本 demo | 说明 |
| --- | --- | --- |
| Python 3.11 + **uv** workspace（core / sdk / predictors） | ✅ | 另拆出 `contracts` 包（§4 契约） |
| **ruff + pyright** + pre-commit、**pytest + hypothesis** | ✅ | hypothesis 性质测试：因果采样不受未来报文影响 |
| **pydantic-settings + YAML** | ✅ | `config/platform.yaml`，`ENVSHIP_*` 环境变量覆盖 |
| **Redis Streams**（consumer group + 修剪） | ✅ | `XADD MINID` 按主题保留时长修剪；预测器副本共享 consumer group 分流 |
| **msgpack + Arrow IPC**，pydantic 导出 JSON Schema | ✅ | `packages/contracts/schemas/*.schema.json` 供非 Python 预测器 |
| tracker 状态 **Redis hash per MMSI（TTL 2 h）** | ✅ | 重启即刻恢复（含待出真值的窗口） |
| features **byte-parity 启动门** | ✅ | golden 样本不一致则拒绝启动；Docker 构建时也校验 |
| **parquet + zstd，Hive 分区，原子 rename** | ✅ | raw_ais / windows / predictions / truth / scores / leaderboard_hourly / sla_hourly |
| **DuckDB** 嵌入式查询、jobs 物化 | ✅ | 用户 SQL 在“加载后禁外部访问 + 锁配置”的快照上执行，单条 SELECT、10 s、10k 行 |
| **APScheduler** jobs，可 `python -m` 手动跑 | ✅ | `python -m envship_core.jobs leaderboard` |
| 预测器 = **独立容器订阅 broker**，SDK `Predictor` Protocol / `run_predictor` / 进程内模式 | ✅ | 5 s 超时记 miss；`packages/sdk/examples/minimal.py` 30 行内 |
| **FastAPI + SSE**（`/stream/positions`、`/stream/scores`） | ✅ | 首帧全量快照 + 差分帧，断线重连不丢状态 |
| **React 18 + Vite + TS，MapLibre + deck.gl，ECharts** | ✅ | 双主题、手机宽度可用；OpenFreeMap 底图 + OpenSeaMap 海图层 |
| **docker compose v2 profiles、Caddy 2、Prometheus/Grafana** | ✅ | `replay` / `live` / `ops` |
| **GitHub Actions** | ✅ | lint → pyright → parity → pytest → 前端构建 → 镜像构建（未推 GHCR） |
| Digitraffic **MQTT push** | ✅ | 另有 REST 轮询兜底（`fi_mode: rest`） |

**Demo 中尚未包含**（需要路线图里提到的外部资源或后续泳道）：Kystdatahuset 挪威 feed、平台自身窗口里的 OSM 环境栅格（`tile_id` 目前为 `none`，`geom` 为零向量；MCM-Net 预测器自带环境瓦片，见下节）、MCM-Net 的 `routed` 部署规则预测器、HF / Zenodo 发布、Loki / Alertmanager / restic 备份、Protomaps 自托管底图、GHCR 推送与外部提交 CI 流程。

## 与模型端（MCM_streaming）的结合

模型端是独立仓库 [`MCM_streaming`](https://github.com/mark000071/MCM_streaming)（MCM-Net 训练、部署服务、论文），本仓库不修改、不复制其代码。

### MCM-Net 预测器（`mcmnet`）

MCM-Net 作为平台的第五个预测器，和 cv / kalman / imm 用同一批窗口、同一个 reconcile 裁判打分，上同一张排行榜。它运行的是 Hugging Face 私有仓 `mark000071/MCM_streaming` 里部署用的 `weights/combined` 模型（记忆库 189,891 条），每个窗口给出 20 条候选，主预测取第一条（与 MCM 部署时的 top-1 规则一致）。

输入与训练完全一致：模型、特征构造和环境栅格化代码都直接调用 MCM_streaming 仓库里的原始实现（`model/models/model_test_trajectory_res.py`、`serving/aisstream/features/`、`serving/aisstream/envtiles/`），只读、不修改。平台窗口里 30 个网格点的经纬度用 MCM 自己的投影公式重新换算；船型暂时一律按 `unknown`（平台还没有接入 AIS 静态报文）；环境输入来自按 MCM 流程从 OSM 生成的瓦片，瓦片覆盖不到的位置按训练时"缺失环境"的约定输入全零，并在该预测的 `model_revision` 上标记 `+no-env`。

```bash
HF_TOKEN=hf_...  scripts/setup_mcmnet.sh        # 克隆 MCM_streaming、下载权重、装 torch、生成芬兰 OSM 瓦片
eval "$(scripts/setup_mcmnet.sh --print-env)"   # 设置 MCM_ROOT / MCM_WEIGHTS / MCM_TILES / MCM_DEVICE
uv run envship dev --fresh --speed 5            # 设置了上述变量就会自动启动 mcmnet（--no-mcmnet 可关闭）
uv run envship benchmark --predictors cv,kalman,imm,mcmnet   # 离线同条件对比，不受实时速度影响
```

- **令牌**：`HF_TOKEN` 只从环境变量读取，不要写进代码或提交。Colab 里建议放在"密钥"（Secrets）里。
- **速度**：CPU 上每个窗口约 0.3–0.8 s，20× 回放产生窗口的速度（约每秒 6 个）超过单进程 CPU 的处理能力，过期的窗口会被跳过、计入缺失。CPU 上用 `--speed 5` 左右或 `--mcmnet-replicas N`；有 GPU 时 `MCM_DEVICE=cuda`（脚本会自动检测）。
- **`uv sync` 会移除 torch**：torch / scipy / osmium 不在工作区锁文件里（CPU 和 GPU 版本来源不同），`uv run` 会保留它们，但单独执行 `uv sync` 后需要重新运行 `scripts/setup_mcmnet.sh`。
- **主预测规则**：目前取第一条候选，即 MCM 部署时记录的 top-1 规则。k-means 候选本身没有排序，所以它的主预测误差明显高于 20 条中最好一条。MCM 部署时真正服务的规则是"学习打分头选候选、与 CV 以 0.1:0.9 混合，再按转向率路由（直航用 Kalman）"。它的 MLP 打分头权重 `scorer_online_final.npz` 目前在 GitHub 和 Hugging Face 上都找不到（HF 上的 `scorer_head.pkl` 是消融用的 GBDT，与 `model/scorer/artifacts/gbdt_ablation.pkl` 相同），补齐后再作为单独的 `mcmnet-routed` 预测器上榜。
- **离线评测** `envship benchmark`：把回放数据完整跑一遍 tracker，所有预测器对同一批窗口同步作答再统一打分，按全部 / 直航 / 转向分层输出误差，适合公平比较；实时覆盖率和延迟仍以在线排行榜为准。

### 过渡方案：MCM_streaming 部署的只读查看器

`backend/` + `frontend/` 是一个只读查看器，直接读取一套正在运行的 MCM_streaming 部署产出的预测（`predictions/*.parquet`）和对账分数（`metrics.sqlite`），展示 served 规则 vs CV/Kalman 以及 MCM-Net 自身的边际贡献；没有真实部署时用内置的合成数据演示。启动：`./scripts/run_demo.sh`（默认 :8090，与本平台的 :8000 / docker 的 :8080 不冲突）。

| 文档 | 内容 |
| --- | --- |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 部署配置要求：环境、硬件、模型推理参数，以及为什么推理不用 GPU |
| [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) | MCM_streaming 部署输出的 JSON / parquet / SQLite 字段契约（查看器的依据） |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 查看器与模型端的边界设计 |

## 目录结构

```
packages/
  contracts/   主题名、消息模型、wire codec（msgpack + Arrow）、parquet schema、JSON Schema
  sdk/         Predictor Protocol、run_predictor 容器 runner、进程内 predict_all、examples/minimal.py
  predictors/  内置基线：cv、kalman（K=3）、imm（CV + 左/右协调转弯，K=3）；mcmnet（MCM-Net，K=20，需另行准备，见上文）
  core/        ingest_fi、replay、tracker(+features)、reconcile、jobs、api、devstack、cli
web/           React 前端
docker/        Dockerfile、web.Dockerfile、Caddyfile、prometheus/grafana 配置
fixtures/      raw_ais（≈40 min 芬兰真实 AIS，CC BY 4.0）、golden/features.npz（parity 样本）
tests/         pytest（契约、特征因果性、预测器、打分、SQL 防护、端到端管道）
docs/roadmap.md  技术路线图原文
backend/ frontend/ demo/ deploy/ scripts/   MCM_streaming 部署的只读查看器（过渡方案，见上节）
```

## 写一个预测器

```python
from envship_sdk import PredictorInfo, TileStore, Window, make_candidates, run_predictor


class MyPredictor:
    def info(self):
        return PredictorInfo(id="mine", version="0.1.0", k=1)

    def warmup(self, tiles: TileStore):
        pass

    def predict(self, w: Window):
        # w.tokens: (30, 8) = x, y(相对锚点 m), sog, cog, sin cog, cos cog, 报文龄 s, 航位推算标志
        return make_candidates(w, paths)  # (K, 30, 2) 相对锚点的东/北位移（m），每步 20 s


run_predictor(MyPredictor())  # 读 REDIS_URL / PREDICTOR_ID / TILE_DIR
```

平台对每个预测器用同一套规则打分：`selected` 候选的 ADE 为榜单主列，best-of-K 为参考列；5 s 内未作答记为 miss（计入 coverage）；coverage ≥ 95% 且 ≥ 20 个已打分窗口才进入排名。

## 开发

```bash
uv run pytest                 # 40 个测试（含 backend/tests 查看器测试）
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run python -m envship_core.features            # parity 自检
uv run python -m envship_core.fixtures rec.jsonl fixtures/raw_ais   # 用新录制的数据重建 fixture
(cd web && npm run dev)       # Vite 开发服务器 :5173，代理到 api :8000
```

## 数据许可

- AIS 位置：Fintraffic / Digitraffic，[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 底图：OpenFreeMap / OpenMapTiles / © OpenStreetMap contributors（ODbL）；海图层 © OpenSeaMap contributors
- 代码：Apache-2.0（见 `LICENSE`）
