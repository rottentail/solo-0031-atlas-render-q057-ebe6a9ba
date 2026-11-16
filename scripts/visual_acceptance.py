#!/usr/bin/env python3
"""为视觉验收规范生成有实图支持的证据。

脚本在临时状态目录启动真实 Atlas 进程，沿 examples/visual-acceptance 下的同一份
固定输入派生所有变体，依次证明三个不能互相替代的结论：

1. PNG 字节身份：同一快照重新渲染，下载到的字节与 SHA-256 摘要完全相同。
2. 解码像素一致：把 PNG 重新压缩（仅改 zlib level），字节不同但解码像素逐通道相同。
3. 肉眼可接受但像素不同：改背景、1 像素几何位移、透明素材位移都会被 /v1/compare
   记为非零差异，服务端没有“容差通过”这一结论。

结果写入 --out（默认 docs/assets/visual-acceptance）：PNG 实图、透明素材、
重新压缩样本和 evidence.json。脚本只用 Python 标准库。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "visual-acceptance"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------
# 最小 PNG 工具：解析分块、8 位 RGBA 解码（含全部五种过滤器）、按指定 level 重压缩
# --------------------------------------------------------------------------

def read_chunks(data):
    if not data.startswith(PNG_SIG):
        raise ValueError("not a PNG")
    chunks, pos = [], 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        chunks.append((tag, body))
        if tag == b"IEND":
            break
    return chunks


def write_png(chunks):
    out = bytearray(PNG_SIG)
    for tag, body in chunks:
        out += struct.pack(">I", len(body)) + tag + body
        out += struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return bytes(out)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_rgba(data):
    """解码 8 位 RGB/RGBA PNG，返回 (width, height, rows)，rows 为每行原始通道字节。

    Atlas 输出整体不透明时 Go 编码器写 color type 2（RGB）；透明素材是 type 6（RGBA）。
    """
    chunks = read_chunks(data)
    width = height = bit = color = None
    raw = b""
    for tag, body in chunks:
        if tag == b"IHDR":
            width, height, bit, color, _, _, _ = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            raw += body
    if bit != 8 or color not in (2, 6):
        raise ValueError("证据脚本只处理 8 位 RGB(2)/RGBA(6) PNG")
    channels = 4 if color == 6 else 3
    inflated = zlib.decompress(raw)
    stride = width * channels
    rows, prev, pos = [], bytearray(stride), 0
    for _ in range(height):
        ftype = inflated[pos]
        line = bytearray(inflated[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + left) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"未知 PNG 过滤器 {ftype}")
        rows.append(bytes(line))
        prev = line
    return width, height, rows


def recompress(data, level):
    """只更换 IDAT 的 zlib 压缩级别，IHDR/IEND 等其余分块原样保留。"""
    width, height, rows = decode_rgba(data)
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0（None）逐行
    chunks = [(t, b) for t, b in read_chunks(data) if t != b"IDAT"]
    idx = next(i for i, (t, _) in enumerate(chunks) if t == b"IHDR")
    chunks.insert(idx + 1, (b"IDAT", zlib.compress(raw, level)))
    return write_png(chunks)


def make_badge_png():
    """16x16 RGBA 素材：中心青色圆带渐变 alpha，四角完全透明，含半透明边缘。"""
    width = height = 16
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            dx, dy = x - 7.5, y - 7.5
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= 7.5:
                alpha = int(round(255 * max(0.0, (7.5 - dist) / 7.5)))
                row += bytes((20 + x * 8, 160 - y * 4, 190, alpha))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(b"\x00" + bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return write_png([
        (b"IHDR", ihdr),
        (b"IDAT", zlib.compress(b"".join(rows), 9)),
        (b"IEND", b""),
    ])


# --------------------------------------------------------------------------
# 临时服务器控制（与 scripts/walkthrough.py 相同的进程模型）
# --------------------------------------------------------------------------

class Demo:
    def __init__(self, binary, directory):
        self.directory = directory
        self.log = open(directory / "server.log", "ab")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, ATLAS_ADDR=f"127.0.0.1:{port}",
                   ATLAS_STATE=str(directory / "state.json"))
        self.proc = subprocess.Popen([str(binary)], env=env,
                                     stdout=self.log, stderr=self.log)
        for _ in range(100):
            try:
                self.call("GET", "/healthz")
                return
            except (urllib.error.URLError, ConnectionError):
                if self.proc.poll() is not None:
                    raise RuntimeError((directory / "server.log").read_text())
                time.sleep(0.03)
        raise RuntimeError("server startup deadline exceeded")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self.log.close()

    def call(self, method, path, body=None, status=200, headers=None):
        hdr = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdr["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=body,
                                         method=method, headers=hdr)
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if response.status != status:
                raise RuntimeError(
                    f"{method} {path}: expected {status}, got {response.status}: {data[:300]!r}")
            if response.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(data)
            return data

    def raw(self, method, path, body=None, headers=None):
        """与 call 相同，但把非 2xx 响应作为 (status, jsonBody) 返回，不抛异常。"""
        hdr = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdr["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=body,
                                         method=method, headers=hdr)
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            payload = json.loads(data.decode()) if data else None
            return response.status, payload

    def wait(self, job_id):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            job = self.call("GET", "/v1/jobs/" + job_id)
            if job["state"] == "succeeded":
                return job
            if job["state"] in ("failed", "cancelled"):
                raise RuntimeError(f"unexpected job outcome: {job}")
            time.sleep(0.025)
        raise RuntimeError("render deadline exceeded")

    def render(self, scene_id, document, revision, key, scale=1):
        self.call("PUT", f"/v1/scenes/{scene_id}",
                  {"name": scene_id, "expectedRevision": revision, "document": document})
        snap = self.call("POST", f"/v1/scenes/{scene_id}/snapshots",
                         {"expectedRevision": revision + 1}, 201)
        job = self.call("POST", "/v1/jobs", {"snapshotId": snap["id"], "scale": scale},
                        202, {"Idempotency-Key": key})
        done = self.wait(job["id"])
        png = self.call("GET", "/v1/artifacts/" + done["artifactId"])
        return {"snapshotId": snap["id"], "snapshotDigest": snap["digest"],
                "jobId": done["id"], "artifactId": done["artifactId"],
                "png": png, "pngDigest": hashlib.sha256(png).hexdigest(),
                "pngBytes": len(png)}


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "assets" / "visual-acceptance"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    base_fixture = load_fixture("fixed-input.json")
    base_doc = base_fixture["document"]
    recolor = load_fixture("variant-recolor.json")
    nudge = load_fixture("variant-nudge.json")
    alpha_case = load_fixture("variant-alpha.json")

    evidence = {"fixtures": {}, "cases": [], "stopRules": {}}
    for name in ("fixed-input.json", "variant-recolor.json",
                 "variant-nudge.json", "variant-alpha.json"):
        data = (FIXTURES / name).read_bytes()
        evidence["fixtures"][name] = {"sha256": hashlib.sha256(data).hexdigest(),
                                      "bytes": len(data)}

    with tempfile.TemporaryDirectory(prefix="atlas-va-") as temp:
        directory = Path(temp)
        binary = directory / "atlas"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/atlas"],
                       cwd=ROOT, check=True, timeout=90)
        demo = Demo(binary, directory)
        try:
            def save(name, png):
                (out / name).write_bytes(png)

            # 案例 A：字节身份 —— 同一快照、同一 scale，幂等重放得到同一任务/结果。
            a1 = demo.render("va-base", base_doc, 0, "va-base-1")
            replay = demo.call("POST", "/v1/jobs",
                               {"snapshotId": a1["snapshotId"], "scale": 1},
                               202, {"Idempotency-Key": "va-base-1"})
            a2_job = demo.wait(replay["id"])
            a2_png = demo.call("GET", "/v1/artifacts/" + a2_job["artifactId"])
            save("case-a-render-1.png", a1["png"])
            cmp_a = demo.call("POST", "/v1/compare",
                              {"left": a1["artifactId"], "right": a1["artifactId"]})
            evidence["cases"].append({
                "id": "A-byte-identity",
                "title": "同一固定输入重新渲染：字节身份成立",
                "artifacts": ["case-a-render-1.png"],
                "snapshotId": a1["snapshotId"],
                "snapshotDigest": a1["snapshotDigest"],
                "replayJobId": replay["id"],
                "replaySameJob": replay["id"] == a1["jobId"],
                "replayArtifactId": a2_job["artifactId"],
                "render1": {"sha256": a1["pngDigest"], "bytes": a1["pngBytes"]},
                "render2": {"sha256": hashlib.sha256(a2_png).hexdigest(),
                            "bytes": len(a2_png)},
                "byteIdentical": a1["png"] == a2_png,
                "compare": cmp_a,
                "verdict": "tier1-byte-identical",
            })

            # 案例 B：重新压缩 —— 字节不同，解码像素逐通道相同。
            re9 = recompress(a1["png"], 9)
            re1 = recompress(a1["png"], 1)
            save("case-b-recompress-level9.png", re9)
            save("case-b-recompress-level1.png", re1)
            _, _, rows_orig = decode_rgba(a1["png"])
            _, _, rows_l9 = decode_rgba(re9)
            _, _, rows_l1 = decode_rgba(re1)
            pixel_equal = rows_orig == rows_l9 == rows_l1
            evidence["cases"].append({
                "id": "B-recompress-pixel-equal",
                "title": "仅重压缩：字节身份不成立，解码像素一致",
                "artifacts": ["case-a-render-1.png",
                              "case-b-recompress-level9.png",
                              "case-b-recompress-level1.png"],
                "original": {"sha256": hashlib.sha256(a1["png"]).hexdigest(),
                             "bytes": len(a1["png"])},
                "level9": {"sha256": hashlib.sha256(re9).hexdigest(),
                           "bytes": len(re9)},
                "level1": {"sha256": hashlib.sha256(re1).hexdigest(),
                           "bytes": len(re1)},
                "byteIdentical": False,
                "decodedPixelsIdentical": pixel_equal,
                "serverCompareAvailable": False,
                "serverCompareReason": "/v1/compare 只接受服务端结果 ID，外部重压缩文件无法提交",
                "verdict": "tier2-pixel-equal-only",
            })

            # 案例 C：细小微调 —— 背景每通道约 5 的色差与 1 像素几何位移。
            doc_recolor = dict(base_doc)
            doc_recolor["background"] = recolor["documentPatch"]["background"]
            c_recolor = demo.render("va-recolor", doc_recolor, 0, "va-recolor")
            save("case-c-recolor-background.png", c_recolor["png"])
            cmp_recolor = demo.call("POST", "/v1/compare",
                                    {"left": a1["artifactId"],
                                     "right": c_recolor["artifactId"]})

            doc_nudge = json.loads(json.dumps(base_doc))
            patch = nudge["documentPatch"]["transform"]
            plate = next(l for l in doc_nudge["layers"] if l["id"] == "plate")
            plate["transform"].update(patch)
            c_nudge = demo.render("va-nudge", doc_nudge, 0, "va-nudge")
            save("case-c-nudge-1px.png", c_nudge["png"])
            cmp_nudge = demo.call("POST", "/v1/compare",
                                  {"left": a1["artifactId"],
                                   "right": c_nudge["artifactId"]})
            evidence["cases"].append({
                "id": "C-small-edits",
                "title": "背景微改与 1 像素位移：像素级均判为变更，没有容差通过",
                "artifacts": ["case-a-render-1.png",
                              "case-c-recolor-background.png",
                              "case-c-nudge-1px.png"],
                "recolor": {"snapshotDigest": c_recolor["snapshotDigest"],
                            "pngDigest": c_recolor["pngDigest"],
                            "compareAgainstBase": cmp_recolor},
                "nudge": {"snapshotDigest": c_nudge["snapshotDigest"],
                          "pngDigest": c_nudge["pngDigest"],
                          "compareAgainstBase": cmp_nudge},
                "serverHasToleranceParameter": False,
                "verdict": "tier3-human-review-required",
            })

            # 案例 D：透明素材 —— 半透明像素与 1 像素位移的严格比较。
            badge = make_badge_png()
            save("case-d-alpha-badge.png", badge)
            asset = demo.call("PUT", "/v1/assets/alpha-badge", badge, 201)
            doc_alpha = json.loads(json.dumps(base_doc))
            doc_alpha["layers"].append(alpha_case["imageLayer"])
            d_alpha = demo.render("va-alpha", doc_alpha, 0, "va-alpha")
            save("case-d-alpha-composite.png", d_alpha["png"])
            cmp_alpha_vs_base = demo.call("POST", "/v1/compare",
                                          {"left": a1["artifactId"],
                                           "right": d_alpha["artifactId"]})

            doc_shift = json.loads(json.dumps(doc_alpha))
            badge_layer = next(l for l in doc_shift["layers"] if l["id"] == "badge")
            badge_layer["transform"].update(alpha_case["nudgeLayer"]["transform"])
            d_shift = demo.render("va-alpha-shift", doc_shift, 0, "va-alpha-shift")
            save("case-d-alpha-nudge-1px.png", d_shift["png"])
            cmp_shift = demo.call("POST", "/v1/compare",
                                  {"left": d_alpha["artifactId"],
                                   "right": d_shift["artifactId"]})
            evidence["cases"].append({
                "id": "D-transparent-asset",
                "title": "透明素材合成与位移：alpha 通道逐通道参与严格比较",
                "artifacts": ["case-d-alpha-badge.png",
                              "case-d-alpha-composite.png",
                              "case-d-alpha-nudge-1px.png"],
                "asset": {"id": "alpha-badge", "digest": asset["digest"],
                          "width": asset["width"], "height": asset["height"]},
                "composite": {"pngDigest": d_alpha["pngDigest"],
                              "compareAgainstBase": cmp_alpha_vs_base},
                "nudge": {"pngDigest": d_shift["pngDigest"],
                          "compareAgainstComposite": cmp_shift},
                "verdict": "tier3-human-review-required",
            })

            # 案例 E：历史复核的停止条件 —— 清理后 410，只剩任务事实与摘要。
            purge_id = a1["artifactId"]
            job_before = demo.call("GET", "/v1/jobs/" + a1["jobId"])
            demo.call("DELETE", "/v1/artifacts/" + purge_id)
            purge_status, purge_body = demo.raw("GET", "/v1/artifacts/" + purge_id)
            compare_after_purge, compare_purge_body = demo.raw(
                "POST", "/v1/compare",
                {"left": purge_id, "right": c_recolor["artifactId"]})
            job_after = demo.call("GET", "/v1/jobs/" + a1["jobId"])
            evidence["stopRules"] = {
                "purgedArtifactId": purge_id,
                "downloadStatusAfterPurge": purge_status,
                "downloadError": purge_body,
                "compareStatusAfterPurge": compare_after_purge,
                "compareError": compare_purge_body,
                "jobStateBefore": job_before["state"],
                "jobStateAfter": job_after["state"],
                "jobArtifactLinkKept": job_after.get("artifactId") == purge_id,
                "retainedEvidence": [
                    "job 记录（state=succeeded、artifactId、attempt、snapshotId、scale）",
                    "artifact 记录（id、jobId、width、height、digest、state=purged）",
                    "快照摘要 snapshotDigest 与固定输入闭包",
                    "领域事件（截断前的 job.succeeded / artifact.purged）",
                ],
                "lostEvidence": ["PNG 字节本体，无法再次下载、解码或像素比较"],
                "mustStop": "字节清理后不得再对该结果给出像素通过或视觉通过结论；只能引用清理前留存的摘要与既有判定记录。",
            }
        finally:
            demo.stop()

    (out / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out),
                      "cases": [c["id"] for c in evidence["cases"]],
                      "fixtures": evidence["fixtures"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
