# 示例

- `emblem.json`：可直接 `PUT /v1/scenes/emblem` 的完整场景请求（双色印记）。
- `visual-acceptance/`：视觉验收固定输入与变体。`fixed-input.json` 与 `emblem.json`
  是同一份场景文档，视觉验收的所有变体（改背景、1 像素位移、透明素材）都从它派生，
  使读者能沿同一份固定输入追溯每个判定依据。各文件记录了 `derivedFrom` 与具体改动，
  实测图片、摘要和比较指标由 `scripts/visual_acceptance.py` 生成，
  规范与停止规则见 `../docs/visual-acceptance.md`。
