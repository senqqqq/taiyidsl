# Yb171 基准测试：文献、教程实现与实验边界

本页配合 [Yb171 tune-up 教程](yb171_global_rf_tuneup.ipynb) 阅读。
教程控制条件是 **全局 RF、同一区域共享 clock/UV**，编码在基态核自旋；
每个完整门包含 clock shelving、UV 控制和 clock unshelving。
这里的“已实现”指可离线构建的实验序列、理想矩阵核验和分析示例，
不表示已经接入真实相机、完成原子校准或测得门保真度。

## 原论文对应什么

| 论文 | 与本教程直接相关的内容 | 迁移边界 |
| --- | --- | --- |
| J. A. Muniz et al., **High-Fidelity Universal Gates in the 171Yb Ground-State Nuclear-Spin Qubit**, PRX Quantum **6**, 020334 (2025)，Atom Computing；[正文与附录](https://harvest.aps.org/v2/journals/articles/10.1103/PRXQuantum.6.020334/fulltext) | §IV 的完整基态门、clock/CZ 对照、U-GERB；Fig. 3 与 Appendix E 的波形参数优化 | U-GERB 使用共同 Haar 随机旋转；论文还具备独立单比特控制，可做完整两比特 Clifford RB。本教程的控制条件不同。 |
| Shuo Ma et al., **High-fidelity gates and mid-circuit erasure conversion in an atomic qubit**, Nature **622**, 279–284 (2023)；[正式论文](https://doi.org/10.1038/s41586-023-06438-1)、[作者预印本与 Methods](https://arxiv.org/pdf/2305.05493) | 全局随机电路、Bell 测量、输入态相关泄漏；多能级模型下重新优化 UV 相位 | 编码在亚稳态核自旋；额外检测光和读出实现中途 erasure。所链预印本使用较早标题；这里只迁移协议思路，不混用版本数值或把其读出修正移到基态编码。 |
| Ran Finkelstein et al., **Universal quantum operations and ancilla-based read-out for tweezer clocks**, Nature **634**, 321–327 (2024)，Endres 组；[正式论文](https://doi.org/10.1038/s41586-024-08005-8)、[作者全文](https://arxiv.org/pdf/2402.16220) | Fig. 2、Methods 与 Extended Data Fig. 3 的对称子空间基准、Bell 人口/宇称及读出泄漏修正 | 原体系是 Sr 光钟比特。固定单比特门数量仍不足以消除所有单比特误差偏差；读出将泄漏判作亮态时，需独立校正。 |
| Richard Bing-Shiun Tsai et al., **Benchmarking and Fidelity Response Theory of High-Fidelity Rydberg Entangling Gates**, PRX Quantum **6**, 010331 (2025)，Endres 组；[正文与附录](https://journals.aps.org/prxquantum/pdf/10.1103/PRXQuantum.6.010331) | Fig. 2 的 symmetric stabilizer benchmarking（SSB）及共同相位扫描；Appendix E 的协议偏差分析；Appendix F 的泄漏修正 | SSB 探测对称输入态上的门表现；论文噪声模型与泄漏修正属于其 Sr 实验，不是任意两比特平均保真度公式。 |
| Simon J. Evered et al., **High-fidelity parallel entangling gates on a neutral-atom quantum computer**, Nature **622**, 268–272 (2023)；[正式论文](https://doi.org/10.1038/s41586-023-06481-y) | 连续相位调制门、平滑幅度门、回波与无回波随机序列 | 用于理解波形参数化与校准思路；Rb 两光子激发的中间态、光移和失谐参数不能作为 Yb 单光子 UV 标定。 |

## 现有序列怎样使用

下面的名字来自[生成器](../../scripts/build_yb171_tuneup_notebook.py)；
纯数学门词和理想模型放在[分析示例](../../examples/yb171_tuneup_analysis.py)。
测量顺序仍在 notebook 中直接展示，不要求先理解优化框架。

| 实验入口 | 扫描或对照 | 能回答的问题 |
| --- | --- | --- |
| `cz_echo_phase` | UV 第二段相位；不同奇数放大次数 `M` | 对近似对角门校准纠缠相位；每条含 `2*M` 次完整门。回波会抵消静态局域相位，需下面的无回波对照。 |
| `bell_population`、`bell_parity` | clock 返回相位、共同 RF 分析相位 | 测偶/奇人口及复相干；指定目标 Bell 相位，不能只最大化宇称振幅。它是制备电路的态指标。 |
| `global_gate_word` + `global_echo_word` | 匹配等待、clock 往返、完整 CZ；共享随机 seed | 定位 RF、shelving 和 UV 误差贡献。当前随机集合为共同半 π 的 Clifford 子集，名称保持 **GERB-style**。 |
| `global_gate_word` + `ssb_sequences` | CZ 数量、随机词、均衡覆盖 12 个初态 | 无回波 SSB；总 RF 数固定，CZ 计数包含初始化/恢复。矩阵核验正确恢复，测量后仍须检查模型适用性。 |
| `cz_ssb_phase_scan` | 固定 SSB 深度/训练词，扫共同 clock 返回相位 | 对应 Tsai Fig. 2(b) 的共同相位校准思路；参数冻结后以独立 seed 验证，避免用同一数据选参数又报性能。 |
| `cz_input_loss_scan` | 共同准备 `00/++/11`，扫完整 CZ 次数，无中间 RF | 对应 Ma Fig. 4(e) 的输入依赖诊断思路；测 loss/clock 残留随门数变化，不宣称实现中途 erasure。 |
| `shaped_pair_closure`、`shaped_echo` | 单脉冲候选、幅度与失谐；回波放大 | 分别检查人口返回与纠缠相位，作为候选筛选；两者都不能单独证明 CZ。 |
| `shaped_bell_population`、`shaped_bell_parity`、`shaped_ssb_word` | 共同补偿、分析相位、训练/留出门词 | 将候选带入完整基态门，按相同基准比较；始终同时记录损失与泄漏。 |

严格的 Muniz U-GERB 是每块 `R_random → U → Xπ_global → U`，
其中 `R_random` 为共同 Haar 旋转，最后计算共同恢复旋转。
当前简化随机集合不具有完全相同的协议身份，也不照搬论文的拟合背景或误差扣除。
全局控制基准不能覆盖全部两比特操作；例如仅作用于对称态的理想数据不能排除 SWAP 错误。
这与 Ma Methods 的限制一致，不能把测得衰减直接称为完整两比特 Clifford fidelity。

## CZ 波形优化：先定义候选，再做原子测量

原有两段 `Square` 保留为明确的基线。新增 `uv_shaped_candidates` 使用一个
`FlatTopGaussian` UV pulse，扫描平台长度和光学相位幅度；`play_shaped_gate`
仍负责一整次 clock 往返。总 UV 长度为 `flat_duration + 2*cutoff_sigma*sigma`，
比较门时间时包括两条 Gaussian 边沿、clock 脉冲和间隔。

本教程选择端点平滑的相位家族

\[
u=t/T,\qquad m(u)=16\beta u^2(1-u)^2,
\]

对应 `PolynomialPhase((0, 0, 16*beta, -32*beta, 16*beta))`；系数单位为 rad，
`beta` 经已标定的光学 phase scale 换算后才进入 DSL。
它满足相位和一阶导数在两端归零，使用 `phase_modulation_mode="local"`。
它是**本教程的探索家族**，没有声称已找到 CZ，也不是论文的 time-optimal 波形。
Muniz/Evered 的正弦相位参数化、Ma 的多能级 GRAPE 结果不能靠替换名称复现。

当前 API 还支持 `HalfSinePhase`；它的端点相位为零，但端点导数并非零。
`PolynomialPhase` 按 `u` 的升幂取系数；并非任意数组波形上传接口。
Gaussian 的 `sigma/cutoff_sigma/sample_interval` 是静态参数，改变边沿需重建候选。
细节见[脉冲声明](../../src/taiyidsl/dsl/pulses.py)与[相位语义](../spectrum_phase_modes.md)。

频率加上线性相位导数可能表示同一个失谐自由度；拟合时固定一种表达，避免重复优化。
DAC 幅度不直接等于光学 Rabi 频率，需测量 AOM/放大器/光学响应。
编译后的瞬时频率检查也不证明模拟带宽、边沿或原子处相位已经正确。
候选先审查最终 DAC 样本，再由实验室测量实际光学包络与相位。

建议按以下顺序采数：

1. 用独立的单原子占据样本和成对占据样本检查返回、残留激发及损失。
   改 `targets` 或删除一个 qubit 声明不会在共享光场中制备单原子。
2. 扫候选、幅度和频率，筛掉高泄漏/高损失区域；用多个回波放大次数约束纠缠相位，
   防止只锁到某个等价人口极值。
3. 用 Bell 人口/相位和固定深度 SSB 调共同补偿，再固定所有参数。
4. 用留出 seed、不同深度及相邻幅度/失谐点验证稳定性；按 pair 分析再汇总。

可使用预先固定权重的工程目标

\[
J=w_R(1-P_{return}^{pre})+w_LP_{loss}+w_CP_{clock}
  +w_\phi[1-\cos(\beta_{Bell}-\beta_{target})].
\]

这里 `pre` 表示只对实验开始时有效原子对条件化；终态丢失不从分母删掉。
`J` 是选参评分，多个项可能重叠计入错误，**不是门 infidelity**。
同时保存每项原始统计；也可先限定损失/泄漏上限，再比较返回与相位，不必压成一个数。
没有真实测量数据时，只生成候选和采数计划，不输出“最优门”或优化成功结论。

候选清单和训练/留出词分别保存为 `shaped_candidate_manifest.json`、
`shaped_ssb_train.json`、`shaped_ssb_heldout.json`。
它们是可复现实验输入，不是测量记录。Hessian 方向扫描可在有可信局域模型和真实反馈后
加入；当前没有实现 Muniz 的 Hessian 优化器、GRAPE 或噪声谱 fidelity-response 求解器。

## 测量要求与尚未接入的能力

- 保留同一 shot 的 pair、seed、初始化态、门数、候选参数、attempt、各站点状态及帧状态。
  `00/01/10/11`、clock 残留、原子丢失和缺帧分别记录；缺帧不能算 `0` 或原子丢失。
- 同时报初始有效 pair 条件下的原始 return、终态 logical survival、条件 return 和 atom survival。
  阈值/repump/push-out 会改变“亮”的物理含义；只有对应的独立读出校准才能支持泄漏修正。
  Sr 的泄漏修正值、Ma 的 erasure 比例和 Muniz 的背景概率不移植到此配置。
- 对 SSB 按序列/初始化态分层给区间，检查深度窗口、残差和非指数振荡。
  重复同一门词的 shot 不等于独立随机电路；不同深度和候选应交错采集以抑制漂移偏差。
- `cz_input_loss_scan` 已表达 Ma 的 `00/++/11` 输入相关诊断；
  其序列可编译不等于已经具备区分各泄漏态的读出。
- 中途 erasure 检测、逐站点控制、完整两比特 Clifford RB、ancilla 读出和噪声谱测量
  均需额外实验室资源及相应运行路径。当前 native 的首尾 hook、相机 TTL fixture
  不能冒充这些能力；参考[读出契约](../readout_adapters.md)和[原生组件](../native_components.md)。

离线构建、理想模型、真实 ARTIQ 编译、光学传递函数验收和原子基准是不同验证层。
教程只自动完成已声明的离线步骤；真实配置与校准数据由实验室包提供。
